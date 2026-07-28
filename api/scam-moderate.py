import json
import os
import base64
import urllib.request
from http.server import BaseHTTPRequestHandler

REPO = "legaleagles/LabourLaw2"  # single repo for everything — matches scam-ed.py
PENDING_FILE = "scam-reports-pending.json"
PUBLIC_FILE = "scam-reports.json"
GITHUB_API = "https://api.github.com"


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
                try:
                    public_data, public_sha = github_get_raw(PUBLIC_FILE, site_token)
                except Exception:
                    public_data, public_sha = {"entries": []}, None
                # Only the anonymized fields ever go public — the original
                # raw story stays in the pending file, never promoted.
                public_data.setdefault("entries", []).append({
                    "id": target["id"],
                    "category": target["category"],
                    "title": target["title"],
                    "anonymized_story": target["anonymized_story"],
                    "lang": target.get("lang", "en"),
                    "status": "approved",
                })
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
