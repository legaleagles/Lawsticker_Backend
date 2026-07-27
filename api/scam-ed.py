import json
import os
import base64
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

REPO = "legaleagles/LabourLaw2"
BACKEND_REPO = "legaleagles/Lawsticker_Backend"
KB_FILE = "knowledge-base.json"
PENDING_FILE = "scam-reports-pending.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_STORY_LEN = 2000

STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
             "of", "and", "or", "my", "me", "i", "do", "does", "can", "what", "how", "if",
             "it", "this", "that", "be", "have", "has", "will", "should", "would", "am"}


def github_get_raw(path, token, timeout=15, repo=REPO):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
        return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]


def github_put(path, token, content_obj, sha, message, timeout=15, repo=REPO):
    body = json.dumps(content_obj, indent=2, ensure_ascii=False).encode()
    payload = {"message": message, "content": base64.b64encode(body).decode(), "branch": "main"}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
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

Given the user's raw story below, do ALL four of the following and return them in EXACTLY this format, with these exact labels on their own lines (nothing else, no extra commentary, no markdown formatting like ** around the labels themselves):

CATEGORY: [one of: Phone Scam, Online Shopping, Investment, Job Offer, Loan/Financial, Digital/Cyber, Other]
TITLE: [a short 5-8 word anonymized title describing the scam pattern, not the person]
ANONYMIZED_STORY: [rewrite the story removing ALL names, business names, phone numbers, email addresses, specific locations, or any other identifying detail — describe only the technique/pattern used, in 2-4 sentences]
REMEDY_ADVICE: [helpful, practical advice for what the user can do — answer in {lang_names.get(lang, "English")}]

For REMEDY_ADVICE specifically, follow these rules:
- Prefer the APPROVED CONTENT below wherever it's relevant. Specific claims (exact numbers, deadlines, fees, section numbers) must ONLY come from the APPROVED CONTENT.
- If the approved content doesn't cover it, general guidance from your own knowledge of Indian law is fine, but say plainly this part isn't from the site's verified content.
- Keep it concise and practical — a few sentences.
- If a genuinely relevant national helpline exists (fraud/cybercrime: 1930, NALSA legal aid: 15100), mention it.
- If the story doesn't actually describe a scam (sounds like a personal dispute, refund disagreement, etc.), say so honestly in REMEDY_ADVICE and gently suggest that may not be a scam, rather than force a categorization.

APPROVED CONTENT:
{context}

USER'S STORY:
{story}"""
    return prompt


def call_gemini(api_key, prompt):
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 700},
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
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None


def parse_gemini_output(text):
    def extract(label, next_labels):
        # Handles Gemini optionally wrapping labels in markdown bold
        # (**CATEGORY:**), different casing, and extra blank lines —
        # a rigid exact-match regex silently broke on any of these,
        # which is very plausibly why real submissions weren't saving.
        label_pat = rf"\**\s*{label}\s*:\**\s*"
        if next_labels:
            next_pat = "|".join(rf"\**\s*{nl}\s*:\**" for nl in next_labels)
            pattern = rf"{label_pat}(.*?)(?=\n\s*(?:{next_pat})|$)"
        else:
            pattern = rf"{label_pat}(.*)"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    labels = ["CATEGORY", "TITLE", "ANONYMIZED_STORY", "REMEDY_ADVICE"]
    return {
        "category": extract("CATEGORY", labels[1:]),
        "title": extract("TITLE", labels[2:]),
        "anonymized_story": extract("ANONYMIZED_STORY", labels[3:]),
        "remedy_advice": extract("REMEDY_ADVICE", []),
    }


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
        import time
        start_time = time.monotonic()

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

            kb, _ = github_get_raw(KB_FILE, site_token, timeout=2)
            entries = kb.get("entries", [])
            relevant_entries = filter_relevant_entries(story, entries)
            prompt = build_scamed_prompt(story, relevant_entries, lang)

            try:
                raw_output = call_gemini(gemini_key, prompt)
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

            if raw_output is None:
                self._respond(200, {"ok": False, "error": "AI service returned an unexpected response."})
                return

            parsed = parse_gemini_output(raw_output)

            if not parsed["remedy_advice"]:
                self._respond(200, {"ok": False, "error": "Could not process this submission."})
                return

            # Save to the pending queue for moderator review — done before
            # responding (not after) since it's uncertain whether code after
            # wfile.write() reliably continues running on this platform.
            # Skipped entirely if the Gemini call already used most of the
            # time budget — the user's actual reply always outranks getting
            # this one submission logged; a slow save must never risk the
            # response itself hitting Vercel's hard 10s ceiling.
            elapsed = time.monotonic() - start_time
            save_debug = None
            if elapsed < 5:
                try:
                    pending, sha = github_get_raw(PENDING_FILE, site_token, timeout=3, repo=BACKEND_REPO)
                except Exception as ge:
                    pending, sha = {"entries": []}, None
                    save_debug = f"get_failed: {repr(ge)}"
                pending.setdefault("entries", []).append({
                    "id": f"scam-{int(datetime.now(timezone.utc).timestamp())}",
                    "category": parsed["category"] or "Uncategorized",
                    "title": parsed["title"] or "(needs manual review)",
                    "anonymized_story": parsed["anonymized_story"] or "[parsing incomplete — see raw output] " + raw_output[:500],
                    "lang": lang,
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                    "status": "pending",
                })
                pending["entries"] = pending["entries"][-500:]
                try:
                    github_put(PENDING_FILE, site_token, pending, sha, "New scam submission pending review", timeout=4, repo=BACKEND_REPO)
                    if save_debug is None:
                        save_debug = "save_ok"
                except Exception as pe:
                    save_debug = f"put_failed: {repr(pe)}"
            else:
                save_debug = f"skipped_elapsed_{elapsed:.1f}s"

            self._respond(200, {"ok": True, "answer": parsed["remedy_advice"], "_debug_save": save_debug})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})
