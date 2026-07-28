import json
import os
import base64
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

REPO = "legaleagles/LabourLaw2"  # single repo for everything — matches scam-ed.py
PENDING_FILE = "scam-reports-pending.json"
PUBLIC_FILE = "scam-reports.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "scam_type_label": {"type": "string", "description": "Short, well-known name for this scam pattern type, e.g. 'Pyramid Scheme / MLM Fraud'"},
        "modus_operandi": {"type": "string", "description": "2-3 sentences on how this type of scam generically operates, based on well-known patterns"},
        "red_flags": {"type": "array", "items": {"type": "string"}, "description": "3-5 short, practical warning signs to watch for"},
        "relevant_laws": {"type": "array", "items": {"type": "string"}, "description": "Names of well-established Indian Acts/laws relevant to this scam type — ONLY Act names, never case citations"},
        "prevalence_note": {"type": "string", "description": "One honest, qualitative sentence on how common/known this pattern is — no invented statistics"},
        "supportive_note": {"type": "string", "description": "A brief, warm, reassuring message for both the person who experienced this and future readers"},
    },
    "required": ["scam_type_label", "modus_operandi", "red_flags", "relevant_laws", "prevalence_note", "supportive_note"],
}


def call_gemini_enrichment(api_key, category, anonymized_story, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    prompt = f"""You are enriching an approved, anonymized scam report for LawSticker AI's public "Scam Stories & Remedies" education page — a real person will read this to learn and feel supported.

Category: {category}
Story: {anonymized_story}

Produce, in {lang_names.get(lang, "English")}:
- scam_type_label: the well-known name for this pattern (e.g. "Pyramid Scheme / MLM Fraud", "Phishing / OTP Scam")
- modus_operandi: how scams of this general type typically work — genuinely informative, not vague
- red_flags: 3-5 concrete, practical warning signs
- relevant_laws: ONLY the names of well-established Indian Acts/laws relevant to this scam type (e.g. "Consumer Protection Act 2019", "Prize Chits and Money Circulation Schemes (Banning) Act 1978", "Information Technology Act 2000"). Do NOT cite specific court cases, judgments, or rulings — those cannot be verified here and must never be invented.
- prevalence_note: one honest, qualitative sentence on how commonly this pattern is reported — do not invent statistics or percentages
- supportive_note: warm, genuine reassurance — for the person who went through this, and for anyone reading this to learn

Stay factual and general. If you're not confident about a specific law applying, leave it out rather than guess."""

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 700,
            "responseMimeType": "application/json",
            "responseSchema": ENRICH_SCHEMA,
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


def github_get_raw(path, token, timeout=10):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
        return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]


def github_put(path, token, content_obj, sha, message, timeout=10):
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _check_password(self, provided):
        # Fails closed: no configured password means every request is
        # rejected, never a default-open state.
        real_password = os.environ.get("SCAM_MODERATOR_PASSWORD")
        if not real_password:
            return False
        return provided == real_password

    def do_POST(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        if not site_token:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode())
            password = body.get("password", "")
            action = body.get("action")

            if not self._check_password(password):
                self._respond(401, {"ok": False, "error": "Incorrect or unset moderator password."})
                return

            if action == "list":
                try:
                    pending, _ = github_get_raw(PENDING_FILE, site_token)
                except Exception:
                    pending = {"entries": []}
                items = [e for e in pending.get("entries", []) if e.get("status") == "pending"]
                self._respond(200, {"ok": True, "items": items})
                return

            report_id = body.get("id")
            if not report_id:
                self._respond(400, {"ok": False, "error": "No report id provided."})
                return

            pending, pending_sha = github_get_raw(PENDING_FILE, site_token)
            target = None
            for e in pending.get("entries", []):
                if e.get("id") == report_id:
                    target = e
                    break
            if not target:
                self._respond(404, {"ok": False, "error": "Report not found."})
                return

            if action == "approve":
                gemini_key = os.environ.get("GEMINI_API_KEY")
                enrichment = None
                if gemini_key:
                    try:
                        enrichment = call_gemini_enrichment(gemini_key, target["category"], target["anonymized_story"], target.get("lang", "en"))
                    except Exception:
                        enrichment = None  # approval must still succeed even if enrichment fails

                try:
                    public_data, public_sha = github_get_raw(PUBLIC_FILE, site_token)
                except Exception:
                    public_data, public_sha = {"entries": []}, None
                # Only the anonymized fields ever go public. Of the structured
                # fields, only the categorical (dropdown-derived) ones are
                # promoted — those are safe by construction. The free-text
                # fields (offer_claim, suspicion_trigger, extra_details) stay
                # pending-only, since they could still carry something
                # identifying even after the AI's anonymization pass.
                src_fields = target.get("structured_fields", {})
                public_entry = {
                    "id": target["id"],
                    "category": target["category"],
                    "title": target["title"],
                    "anonymized_story": target["anonymized_story"],
                    "signals": {
                        "contact_method": src_fields.get("contact_method", ""),
                        "ask_action": src_fields.get("ask_action", ""),
                        "cost_items": src_fields.get("cost_items", []),
                        "money_range": src_fields.get("money_range", ""),
                    },
                    "lang": target.get("lang", "en"),
                    "status": "approved",
                }
                if enrichment:
                    public_entry["enrichment"] = enrichment
                public_data.setdefault("entries", []).append(public_entry)
                github_put(PUBLIC_FILE, site_token, public_data, public_sha, "Approve scam report")
                target["status"] = "approved"

            elif action == "reject":
                target["status"] = "rejected"

            else:
                self._respond(400, {"ok": False, "error": "Unknown action."})
                return

            github_put(PENDING_FILE, site_token, pending, pending_sha, f"Scam report {action}")
            self._respond(200, {"ok": True})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})
