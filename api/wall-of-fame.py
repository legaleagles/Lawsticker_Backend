"""
Wall of Fame submission handler.

Receives a POST from the Food Detective page when a parent submits their
child's completion for the public Wall of Fame. Runs basic validation and
a bad-word filter, then commits a clean entry directly to wall-of-fame.json
in the main site repo (legaleagles/LabourLaw2) via the GitHub API.

Required environment variable (set in Vercel dashboard, never in code):
  SITE_REPO_TOKEN  - a fine-grained GitHub PAT scoped ONLY to
                      legaleagles/LabourLaw2, with Contents: Read and write.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import re
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = "legaleagles/LabourLaw2"
WALL_FILE = "wall-of-fame.json"
FLAGGED_FILE = "wall-of-fame-flagged.json"
GITHUB_API = "https://api.github.com"

VALID_STORIES = [
    "The Starch Test for Paneer, Khoya & Milk",
    "Honey Authenticity Investigation",
    "The Fruit Ripening Inspection",
    "The Silver vs Aluminium Test",
    "Mustard Oil Safety Awareness",
    "Reading the Bold Nutrition Label",
    "Hidden Sugar Detection",
    "Ice Cream vs Frozen Dessert Labelling",
    "Decoding Advertising Tricks",
]

# Basic safety net only — not exhaustive. Flags obvious bad-faith submissions;
# genuine names are almost never affected. Kept intentionally short and generic
# rather than an exhaustive slur list, since that list itself becomes a misuse risk.
BLOCKED_PATTERNS = [
    r"\bfuck\b", r"\bshit\b", r"\bbitch\b", r"\bass+hole\b", r"\bcunt\b",
    r"\bnigg\w*", r"\bslut\b", r"\bwhore\b", r"\bretard\b", r"\bpussy\b",
    r"\brape\b", r"admin", r"<script", r"http[s]?://", r"\bnull\b", r"\btest\b",
]
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s.\-']{0,19}$")


def is_clean(text):
    lowered = text.lower()
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, lowered):
            return False
    return True


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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
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

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw) if raw else {}
        except Exception:
            self._respond(400, {"ok": False, "error": "Invalid request body."})
            return

        first_name = str(data.get("first_name", "")).strip()
        last_initial = str(data.get("last_initial", "")).strip().rstrip(".")
        story = str(data.get("story", "")).strip()
        consent = bool(data.get("consent", False))

        if not consent:
            self._respond(400, {"ok": False, "error": "Parent/guardian consent is required."})
            return
        if not NAME_RE.match(first_name) or len(first_name) < 1:
            self._respond(400, {"ok": False, "error": "Please enter a valid first name."})
            return
        if not last_initial or not last_initial.isalpha() or len(last_initial) > 1:
            self._respond(400, {"ok": False, "error": "Last initial should be a single letter."})
            return
        if story not in VALID_STORIES:
            self._respond(400, {"ok": False, "error": "Unrecognised story selection."})
            return

        display_name = f"{first_name.strip().title()} {last_initial.upper()}."
        entry = {
            "name": display_name,
            "story": story,
            "date": datetime.now(timezone.utc).strftime("%d %B %Y"),
        }

        token = os.environ.get("SITE_REPO_TOKEN")
        if not token:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            if not is_clean(first_name) or not is_clean(story):
                flagged, sha = github_get(FLAGGED_FILE, token)
                if flagged is None:
                    flagged = {"entries": []}
                flagged["entries"].append(entry)
                github_put(FLAGGED_FILE, token, flagged, sha, "Flagged Wall of Fame submission (auto-filter)")
                # Deliberately generic success response — no signal to a bad-faith
                # submitter about what was filtered or why.
                self._respond(200, {"ok": True})
                return

            wall, sha = github_get(WALL_FILE, token)
            if wall is None:
                wall = {"entries": []}

            # Deduplicate: a repeat submission for the same child name and same
            # story replaces the earlier entry rather than creating a duplicate.
            # Always keeps the most recent attempt.
            wall["entries"] = [
                e for e in wall["entries"]
                if not (e.get("name") == entry["name"] and e.get("story") == entry["story"])
            ]
            wall["entries"].insert(0, entry)
            github_put(WALL_FILE, token, wall, sha, f"Wall of Fame: add/update {display_name}")
            self._respond(200, {"ok": True})

        except Exception as e:
            self._respond(500, {"ok": False, "error": "Could not save submission. Please try again shortly."})
