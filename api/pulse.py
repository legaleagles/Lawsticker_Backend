"""
Community Pulse counter — anonymous, aggregate activity counting.

Tracks two site-wide totals: badges earned (Food Detective) and tricks
spotted (Marketing Gimmicks pages). No names, no identifying data, no
per-user tracking of any kind — every request is a single anonymous "+1"
to one of two running totals, nothing more.

GET  /api/pulse            -> current totals
POST /api/pulse {"type":"badge"|"trick"} -> increments one total, returns new totals

Required environment variable (already set in Vercel from the Wall of Fame
setup — no new variable needed):
  SITE_REPO_TOKEN  - fine-grained GitHub PAT scoped to legaleagles/LabourLaw2,
                      Contents: Read and write.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
import urllib.error

REPO = "legaleagles/LabourLaw2"
PULSE_FILE = "pulse-counts.json"
GITHUB_API = "https://api.github.com"
VALID_TYPES = {"badge", "trick"}


def github_get(path, token):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            content = base64.b64decode(data["content"]).decode()
            return json.loads(content), data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def github_put(path, token, content_obj, sha, message):
    body = json.dumps(content_obj, indent=2, ensure_ascii=False).encode()
    payload = {
        "message": message,
        "content": base64.b64encode(body).decode(),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "https://lawsticker-ai.com")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        token = os.environ.get("SITE_REPO_TOKEN")
        if not token:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return
        try:
            counts, _ = github_get(PULSE_FILE, token)
            if counts is None:
                counts = {"badges_total": 0, "tricks_total": 0}
            self._respond(200, {
                "ok": True,
                "badges_total": counts.get("badges_total", 0),
                "tricks_total": counts.get("tricks_total", 0),
            })
        except Exception:
            self._respond(500, {"ok": False, "error": "Could not read counts."})

    def do_POST(self):
        token = os.environ.get("SITE_REPO_TOKEN")
        if not token:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw) if raw else {}
        except Exception:
            self._respond(400, {"ok": False, "error": "Invalid request body."})
            return

        event_type = str(data.get("type", "")).strip()
        if event_type not in VALID_TYPES:
            self._respond(400, {"ok": False, "error": "Unrecognised event type."})
            return

        try:
            counts, sha = github_get(PULSE_FILE, token)
            if counts is None:
                counts = {"badges_total": 0, "tricks_total": 0}

            key = "badges_total" if event_type == "badge" else "tricks_total"
            counts[key] = counts.get(key, 0) + 1

            github_put(PULSE_FILE, token, counts, sha, f"Pulse: +1 {event_type}")
            self._respond(200, {
                "ok": True,
                "badges_total": counts.get("badges_total", 0),
                "tricks_total": counts.get("tricks_total", 0),
            })
        except Exception:
            # A failed ping should never be visible to the user or block their
            # actual action (earning the badge, spotting the trick) — the
            # counter is a nice-to-have, not a critical path.
            self._respond(200, {"ok": False})
