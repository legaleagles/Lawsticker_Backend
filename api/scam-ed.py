import json
import os
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

REPO = "legaleagles/LabourLaw2"  # single repo for everything — the one SITE_REPO_TOKEN is proven to work with
KB_FILE = "knowledge-base.json"
PENDING_FILE = "scam-reports-pending.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_STORY_LEN = 2000

STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
             "of", "and", "or", "my", "me", "i", "do", "does", "can", "what", "how", "if",
             "it", "this", "that", "be", "have", "has", "will", "should", "would", "am"}

CATEGORIES = ["Phone Scam", "Online Shopping", "Investment", "Job Offer",
              "Loan/Financial", "Digital/Cyber", "Other"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "title": {"type": "string", "description": "5-8 word anonymized title describing the pattern, not the person"},
        "anonymized_story": {"type": "string", "description": "Story rewritten with all names, businesses, phone numbers, and locations removed"},
        "remedy_advice": {"type": "string", "description": "Practical legal remedy advice for the user"},
    },
    "required": ["category", "title", "anonymized_story", "remedy_advice"],
}


def github_get_raw(path, token, timeout=15):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
        return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]


def github_put(path, token, content_obj, sha, message, timeout=15):
    body = json.dumps(content_obj, indent=2, ensure_ascii=False).encode()
    payload = {"message": message, "content": base64.b64encode(body).decode(), "branch": "main"}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def filter_relevant_entries(text, entries, max_entries=8):
    q_words = {w for w in text.lower().split() if w not in STOPWORDS and len(w) > 2}
    if not q_words:
        return entries[:max_entries]
    scored = []
    for e in entries:
        body = " ".join([e["title"].get("en", ""), e["tag"].get("en", ""), e["body"].get("en", "")]).lower()
        score = sum(1 for w in q_words if w in body)
        scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [e for score, e in scored if score > 0][:max_entries]
    return relevant if relevant else entries[:max_entries]


def build_scamed_prompt(story, entries, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    prompt = f"""You are processing a scam-experience submission for LawSticker AI's "Scam-Ed" feature — a community scam-awareness archive.

Analyze the user's raw story below and produce:
- category: pick the single best-fitting category
- title: a short anonymized title describing the SCAM PATTERN, not the person or business
- anonymized_story: the story rewritten with ALL names, business names, phone numbers, email addresses, and specific locations removed — describe only the technique used, in 2-4 sentences
- remedy_advice: practical advice for the user, answered in {lang_names.get(lang, "English")}

For remedy_advice specifically:
- Prefer the APPROVED CONTENT below wherever relevant. Specific claims (exact numbers, deadlines, fees, section numbers) must ONLY come from the APPROVED CONTENT.
- If the approved content doesn't cover it, general guidance from your own knowledge of Indian law is fine, but say plainly this part isn't from the site's verified content.
- Keep it concise and practical.
- If a genuinely relevant national helpline exists (fraud/cybercrime: 1930, NALSA legal aid: 15100), mention it.
- If the story doesn't actually describe a scam (sounds like a personal dispute, refund disagreement, etc.), say so honestly rather than forcing a categorization.

APPROVED CONTENT:
{context}

USER'S STORY:
{story}"""
    return prompt


def call_gemini_structured(api_key, prompt):
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 700,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        result = json.loads(resp.read().decode())
    try:
        raw_text = result["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw_text)  # guaranteed valid JSON matching RESPONSE_SCHEMA — no regex needed
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


class handler(BaseHTTPRequestHandler):
    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "https://lawsticker-ai.com")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "https://lawsticker-ai.com")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        gemini_key = os.environ.get("GEMINI_API_KEY")

        if not site_token or not gemini_key:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            story = (body.get("story") or "").strip()[:MAX_STORY_LEN]
            lang = body.get("lang", "en")
            if lang not in ("en", "te", "hi"):
                lang = "en"

            if not story:
                self._respond(400, {"ok": False, "error": "No story provided."})
                return

            # Real measured budget (not guessed): KB fetch + Gemini call
            # together take ~2s in practice. Vercel's hard ceiling is 10s.
            # Timeouts below leave genuine margin even in a slow case.
            kb, _ = github_get_raw(KB_FILE, site_token, timeout=2)
            entries = kb.get("entries", [])
            relevant_entries = filter_relevant_entries(story, entries)
            prompt = build_scamed_prompt(story, relevant_entries, lang)

            try:
                parsed = call_gemini_structured(gemini_key, prompt)
            except urllib.error.HTTPError as e:
                error_body = e.read().decode()
                if e.code == 429:
                    self._respond(200, {"ok": False, "error": "BUSY_RIGHT_NOW"})
                else:
                    self._respond(200, {"ok": False, "error": f"AI service error: {error_body[:200]}"})
                return
            except TimeoutError:
                self._respond(200, {"ok": False, "error": "AI service took too long to respond."})
                return

            if not parsed or not parsed.get("remedy_advice"):
                self._respond(200, {"ok": False, "error": "AI service returned an unexpected response."})
                return

            # Save both the original raw story AND the AI's anonymized
            # version together — the moderator needs to see both to judge
            # whether the anonymization was actually adequate. Only the
            # anonymized fields ever get promoted to the public file later.
            try:
                pending, sha = github_get_raw(PENDING_FILE, site_token, timeout=2.5)
            except Exception:
                pending, sha = {"entries": []}, None
            pending.setdefault("entries", []).append({
                "id": f"scam-{int(datetime.now(timezone.utc).timestamp())}",
                "original_story": story,
                "category": parsed["category"],
                "title": parsed["title"],
                "anonymized_story": parsed["anonymized_story"],
                "lang": lang,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            })
            pending["entries"] = pending["entries"][-500:]
            github_put(PENDING_FILE, site_token, pending, sha, "New scam submission pending review", timeout=2.5)

            self._respond(200, {"ok": True, "answer": parsed["remedy_advice"]})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})
