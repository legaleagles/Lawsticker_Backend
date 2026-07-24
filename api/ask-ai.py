"""
Ask AI — answers user questions using ONLY LawSticker AI's own published,
reviewed content (the Rights Hub knowledge base), via Gemini's free-tier
API. Never invents legal conclusions beyond what the site has published.

Flow: user question in -> fetch knowledge-base.json from GitHub -> build a
constrained prompt (approved content + explicit "don't invent" instruction)
-> call Gemini -> return answer with the source page(s) it drew from, so
the user can click through and read the original.

Required environment variables (SITE_REPO_TOKEN already set; add new):
  SITE_REPO_TOKEN, GEMINI_API_KEY
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = "legaleagles/LabourLaw2"
KB_FILE = "knowledge-base.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

MAX_QUESTION_LEN = 500


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


STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
             "of", "and", "or", "my", "me", "i", "do", "does", "can", "what", "how", "if",
             "it", "this", "that", "be", "have", "has", "will", "should", "would", "am"}


def filter_relevant_entries(question, entries, max_entries=10):
    """
    Sending all 27 knowledge-base entries on every request makes the prompt
    unnecessarily large, which slows Gemini's response time and makes it
    more likely to bump against the function's time budget. This scores
    entries by simple keyword overlap with the question and keeps only the
    most relevant ones, falling back to everything if nothing scores well
    (e.g. a very short or unusual question) so real coverage never
    regresses because of this shortcut.
    """
    q_words = {w for w in question.lower().split() if w not in STOPWORDS and len(w) > 2}
    if not q_words:
        return entries[:max_entries]

    scored = []
    for e in entries:
        text = " ".join([
            e["title"].get("en", ""), e["tag"].get("en", ""), e["body"].get("en", ""),
        ]).lower()
        score = sum(1 for w in q_words if w in text)
        scored.append((score, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [e for score, e in scored if score > 0][:max_entries]
    return relevant if relevant else entries[:max_entries]


def build_prompt(question, entries, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    prompt = f"""You are answering a question for a visitor to LawSticker AI, an Indian legal-rights education website.

Answer using the APPROVED CONTENT below wherever it's relevant. For anything the approved content doesn't cover, you may still help using your own general knowledge of Indian law — the line that matters is NOT the topic, it's the type of claim within your answer:

- SPECIFIC CLAIMS (exact numbers, deadlines, fees, compensation amounts, section numbers, filing procedures, forms) must ONLY ever come from the APPROVED CONTENT below. Never invent or infer a specific figure or deadline that isn't stated there.
- GENERAL GUIDANCE (what kind of remedy exists, which body to approach, broad concepts, what a law is generally about) can come from your own knowledge of Indian law when the approved content doesn't cover the topic — this is genuinely useful even without site-verified specifics.

Decide per answer, honestly, which case you're in:
- If your answer relies only on the APPROVED CONTENT (even if you also add general context around it), end with: [Source: page-name] (the exact page name from the content used).
- If any part of your answer draws on your own general knowledge because the approved content didn't cover it, end with exactly: [General Knowledge] instead — and say plainly in the answer that this part isn't from the site's verified content.
- If you're not confident either way, say so honestly and suggest a professional or legal aid clinic. Do not guess at specific figures under any circumstances.

IMPORTANT: Output ONLY the final answer itself. Do not show your classification, reasoning, or any meta-commentary about which case applies — the person should just see a clean, direct answer.

RULES THAT APPLY EITHER WAY:
- Answer in {lang_names.get(lang, "English")}.
- Keep the answer concise and practical — a few sentences, not an essay.
- Never state a specific number, deadline, or amount that isn't in the approved content, even inside an otherwise general-knowledge answer.

APPROVED CONTENT:
{context}

USER QUESTION: {question}"""
    return prompt


def build_bill_prompt(entries, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        if e["source_page"] != "rights-consumer":
            continue
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"Title: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    prompt = f"""You are looking at an uploaded restaurant/shop bill (photo or document) for a visitor to LawSticker AI, an Indian consumer-rights education website.

Using ONLY the approved consumer-rights content below, check the bill for common issues and explain what you find in plain, practical language:
- Is there a "service charge" line item? If so, note that service charge is optional in India (per CCPA Guidelines 2022) and the customer can ask for it to be removed.
- Do the individual item prices and totals add up correctly? Point out any arithmetic mismatch you can actually see in the image.
- Is there anything charged that looks unusual or unclearly labeled?

STRICT RULES:
- Only state legal facts that appear explicitly in the approved content below. Never invent legal information not stated here.
- Only comment on what you can actually see in the image — do not guess at numbers you cannot read clearly.
- Answer in {lang_names.get(lang, "English")}.
- Keep it concise and practical.
- End with: [Source: rights-consumer]

APPROVED CONTENT:
{context}"""
    return prompt


def call_gemini(api_key, prompt, image_base64=None, image_mime_type=None):
    parts = [{"text": prompt}]
    if image_base64:
        parts.append({"inline_data": {"mime_type": image_mime_type or "image/jpeg", "data": image_base64}})
    payload = json.dumps({
        "contents": [{"parts": parts}],
        # Output tokens cost roughly 6x input tokens — capping length keeps
        # both cost and response time predictable, and reinforces the
        # "concise, a few sentences" instruction already in the prompt.
        "generationConfig": {"maxOutputTokens": 600},
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        result = json.loads(resp.read().decode())
    try:
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        gemini_key = os.environ.get("GEMINI_API_KEY")

        if not site_token or not gemini_key:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            question = (body.get("question") or "").strip()[:MAX_QUESTION_LEN]
            lang = body.get("lang", "en")
            if lang not in ("en", "te", "hi"):
                lang = "en"
            image_base64 = body.get("image_base64")
            image_mime_type = body.get("image_mime_type")

            if not question and not image_base64:
                self._respond(400, {"ok": False, "error": "No question or image provided."})
                return

            kb, _ = github_get_raw(KB_FILE, site_token, timeout=1.5)
            entries = kb.get("entries", [])

            if image_base64:
                prompt = build_bill_prompt(entries, lang)
            else:
                relevant_entries = filter_relevant_entries(question, entries)
                prompt = build_prompt(question, relevant_entries, lang)

            try:
                answer = call_gemini(gemini_key, prompt, image_base64, image_mime_type)
            except urllib.error.HTTPError as e:
                error_body = e.read().decode()
                if e.code == 429:
                    self._respond(200, {"ok": False, "error": f"BUSY_RIGHT_NOW: {error_body[:200]}"})
                else:
                    self._respond(200, {"ok": False, "error": f"AI service error: {error_body[:300]}"})
                return
            except TimeoutError:
                self._respond(200, {"ok": False, "error": "AI service took too long to respond."})
                return

            if answer is None:
                self._respond(200, {"ok": False, "error": "AI service returned an unexpected response."})
                return

            self._respond(200, {"ok": True, "answer": answer})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "https://lawsticker-ai.com")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "https://lawsticker-ai.com")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
