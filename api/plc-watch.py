"""
Pendekanti Law College (plchyd.ac.in) homepage watcher.

Runs on a daily schedule via Vercel Cron. Fetches the college's public
homepage, strips it down to plain text, and compares it against the last
seen version. If anything changed — a new Notice Board entry, a new link,
anything — it sends the current homepage text straight to Telegram.

Deliberately monitors the WHOLE homepage rather than trying to target the
Notice Board section specifically by its HTML structure — that structure
isn't guaranteed to stay the same, and a whole-page comparison keeps
working even if the college's developer changes the page layout entirely.

Required environment variables (already set in Vercel from earlier setup):
  SITE_REPO_TOKEN     - GitHub PAT scoped to legaleagles/LabourLaw2, for
                         storing the "last seen" state.
  TELEGRAM_BOT_TOKEN  - Telegram bot token.
  TELEGRAM_CHAT_ID    - Telegram chat ID to send alerts to.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import re
import base64
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = "legaleagles/LabourLaw2"
STATE_FILE = "plc-watch-state.json"
GITHUB_API = "https://api.github.com"
WATCH_URL = "https://plchyd.ac.in/"
MAX_TELEGRAM_LEN = 3800


def fetch_page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerWatch/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    # Strip scripts/styles entirely, then strip remaining tags, collapse whitespace.
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&quot;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

        if not site_token or not bot_token or not chat_id:
            self._respond(500, {"ok": False, "error": "Server misconfiguration — missing env vars."})
            return

        try:
            page_text = fetch_page_text(WATCH_URL)
            current_hash = hashlib.sha256(page_text.encode()).hexdigest()

            state, sha = github_get(STATE_FILE, site_token)
            is_first_run = state is None
            previous_hash = state.get("last_hash") if state else None

            changed = is_first_run or (current_hash != previous_hash)

            if changed:
                snippet = page_text[:MAX_TELEGRAM_LEN]
                if is_first_run:
                    message = (
                        "🔍 Now watching plchyd.ac.in — this is the baseline snapshot. "
                        "You'll get an alert here the next time anything on the homepage changes.\n\n"
                        + snippet
                    )
                else:
                    message = (
                        "🔔 plchyd.ac.in homepage has changed!\n"
                        "https://plchyd.ac.in/\n\n"
                        + snippet
                    )
                try:
                    send_telegram(bot_token, chat_id, message)
                    telegram_sent = True
                except Exception:
                    telegram_sent = False
            else:
                telegram_sent = None

            new_state = {
                "last_hash": current_hash,
                "last_checked_at": datetime.now(timezone.utc).isoformat(),
                "last_changed_at": datetime.now(timezone.utc).isoformat() if changed else (state.get("last_changed_at") if state else None),
            }
            github_put(STATE_FILE, site_token, new_state, sha, "PLC watch: " + ("change detected" if changed else "no change"))

            self._respond(200, {"ok": True, "changed": changed, "telegram_sent": telegram_sent})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
