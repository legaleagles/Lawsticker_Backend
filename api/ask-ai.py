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

REPO = "legaleagles/LabourLaw2"
KB_FILE = "knowledge-base.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

MAX_QUESTION_LEN = 500


def github_get_raw(path, token):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        return json.loads(base64.b64decode(data["content"]).decode())


def build_prompt(question, entries, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    prompt = f"""You are answering a question for a visitor to LawSticker AI, an Indian legal-rights education website.

PRIORITY ORDER:
1. First, check the APPROVED CONTENT below. If it covers the question, answer using ONLY facts, figures, deadlines, and legal provisions stated there. Never invent or add anything beyond what's written. End with: [Source: page-name] (using the exact page name from the content you used).
2. If the approved content does NOT cover the question, you may answer using your own general knowledge of Indian law instead — but you MUST clearly say this is general knowledge, not verified content from this site, and recommend the person confirm specifics with a professional or legal aid clinic before relying on it. End that kind of answer with exactly: [General Knowledge] instead of a Source tag.
3. If you are not confident even in general knowledge terms, say so honestly rather than guessing, and suggest a professional or legal aid clinic.

RULES THAT APPLY EITHER WAY:
- Answer in {lang_names.get(lang, "English")}.
- Keep the answer concise and practical — a few sentences, not an essay.
- Never blend unverified general knowledge into an answer that's otherwise using approved content — pick one mode per answer and label it correctly.

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
        "contents": [{"parts": parts}]
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
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

            kb = github_get_raw(KB_FILE, site_token)
            entries = kb.get("entries", [])

            if image_base64:
                prompt = build_bill_prompt(entries, lang)
            else:
                prompt = build_prompt(question, entries, lang)

            try:
                answer = call_gemini(gemini_key, prompt, image_base64, image_mime_type)
            except urllib.error.HTTPError as e:
                error_body = e.read().decode()
                self._respond(200, {"ok": False, "error": f"AI service error: {error_body[:300]}"})
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
