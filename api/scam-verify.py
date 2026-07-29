import json
import os
import base64
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

REPO = "legaleagles/LabourLaw2"
PUBLIC_FILE = "scam-reports.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_INPUT_LEN = 1500

RISK_LEVELS = ["High Concern", "Some Concern", "Low Concern", "Not Enough Information"]

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": RISK_LEVELS},
        "red_flags_found": {"type": "array", "items": {"type": "string"}, "description": "Specific warning signs identified in THIS situation, empty array if none"},
        "matches_known_pattern": {"type": "boolean", "description": "True only if this genuinely resembles a pattern in the provided reported cases"},
        "matched_category": {"type": "string", "description": "Which category it resembles, empty string if matches_known_pattern is false"},
        "reasoning": {"type": "string", "description": "2-3 sentences explaining the assessment, plain and simple language"},
        "advice": {"type": "string", "description": "Practical next steps — what to check or do, plain simple language"},
    },
    "required": ["risk_level", "red_flags_found", "matches_known_pattern", "matched_category", "reasoning", "advice"],
}


def github_get_raw(path, token, timeout=15):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
        return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]


def summarize_known_patterns(entries, max_entries=20):
    """
    Condenses the real, approved scam reports into a compact reference the
    model can genuinely check a new situation against — this is what makes
    "matches a known pattern" a grounded claim instead of a guess.
    """
    lines = []
    for e in entries[-max_entries:]:
        enrichment = e.get("enrichment", {})
        signals = e.get("signals", {})
        line = f"- Category: {e.get('category', 'Other')}"
        if enrichment.get("scam_type_label"):
            line += f" | Type: {enrichment['scam_type_label']}"
        if signals.get("contact_method"):
            line += f" | Contact: {signals['contact_method']}"
        if signals.get("ask_action"):
            line += f" | Asked for: {signals['ask_action']}"
        lines.append(line)
    return "\n".join(lines) if lines else "No reported cases in the database yet."


def build_verify_prompt(situation, known_patterns_summary, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    prompt = f"""You are LawSticker AI's "Scam Verify" tool — someone is describing a situation they're currently facing and wants an honest assessment of whether it looks like a scam.

REPORTED PATTERNS FROM OUR COMMUNITY DATABASE (real, anonymized cases):
{known_patterns_summary}

USER'S SITUATION:
{situation}

Analyze honestly and produce, in {lang_names.get(lang, "English")}:
- risk_level: your genuine, calibrated assessment. Do NOT default to "High Concern" just to be safe — only use it when the situation clearly shows real warning signs. Use "Not Enough Information" honestly when the description is too vague to judge.
- red_flags_found: concrete warning signs actually present in what they described — do not invent flags that aren't there
- matches_known_pattern: true ONLY if this situation genuinely resembles one of the reported patterns above — do not force a match
- matched_category: which category, only if matches_known_pattern is true
- reasoning: explain your assessment plainly — why you landed on this risk level
- advice: practical next steps in simple language — what to verify, who to contact if genuinely worried (mention Cybercrime Helpline 1930 or NALSA 15100 only if genuinely relevant to their situation)

Be honest and calibrated — false alarms erode trust just as much as missed warnings. If this looks like a completely normal, legitimate interaction, say so plainly rather than manufacturing concern."""
    return prompt


def call_gemini_verify(api_key, prompt):
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 600,
            "responseMimeType": "application/json",
            "responseSchema": VERIFY_SCHEMA,
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
        return json.loads(raw_text)
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
            situation = (body.get("situation") or "").strip()[:MAX_INPUT_LEN]
            lang = body.get("lang", "en")
            if lang not in ("en", "te", "hi"):
                lang = "en"

            if not situation:
                self._respond(400, {"ok": False, "error": "No situation provided."})
                return

            try:
                public_data, _ = github_get_raw(PUBLIC_FILE, site_token, timeout=2.5)
                entries = public_data.get("entries", [])
            except Exception:
                entries = []
            known_patterns_summary = summarize_known_patterns(entries)

            prompt = build_verify_prompt(situation, known_patterns_summary, lang)

            try:
                result = call_gemini_verify(gemini_key, prompt)
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

            if not result:
                self._respond(200, {"ok": False, "error": "AI service returned an unexpected response."})
                return

            self._respond(200, {"ok": True, "result": result})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})
