"""
Visit Activity Digest — reports how many new visits the site has gotten
since the last check, straight to Telegram. Meant to run every hour or
every 4 hours via an external scheduler (cron-job.org) — Vercel's own
free-tier cron only supports once-daily, which is too infrequent for this.

Purely internal data: reads the same pulse-counts.json already populated
by the homepage's visit ping (extended pulse.py). No third-party site
involved at all.

Required environment variables (already set in Vercel):
  SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = "legaleagles/LabourLaw2"
PULSE_FILE = "pulse-counts.json"
STATE_FILE = "visit-digest-state.json"
GITHUB_API = "https://api.github.com"


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
    payload = {"message": message, "content": base64.b64encode(body).decode(), "branch": "main"}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def send_telegram_to_all(bot_token, chat_id_config, text):
    chat_ids = [c.strip() for c in chat_id_config.split(",") if c.strip()]
    results = {}
    for cid in chat_ids:
        try:
            send_telegram(bot_token, cid, text)
            results[cid] = "sent"
        except Exception as e:
            results[cid] = str(e)
    return results


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

        if not site_token or not bot_token or not chat_id:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            pulse, _ = github_get(PULSE_FILE, site_token)
            current_visits = (pulse or {}).get("visits_total", 0)
            current_badges = (pulse or {}).get("badges_total", 0)
            current_tricks = (pulse or {}).get("tricks_total", 0)

            state, sha = github_get(STATE_FILE, site_token)
            is_first_run = state is None
            last_visits = state.get("last_visits_total", 0) if state else 0

            new_visits = current_visits - last_visits

            # Only alert if there's genuinely something to report — silence
            # on a quiet hour is fine, no need to spam "0 new visits".
            if new_visits > 0 or is_first_run:
                if is_first_run:
                    message = (
                        f"👀 Now tracking site visits.\n\n"
                        f"Current totals — Visits: {current_visits} · Badges: {current_badges} · Tricks spotted: {current_tricks}"
                    )
                else:
                    now = datetime.now(timezone.utc).strftime("%d %b, %H:%M UTC")
                    message = (
                        f"👀 <b>{new_visits} new visit{'s' if new_visits != 1 else ''}</b> since last check\n"
                        f"({now})\n\n"
                        f"Site totals — Visits: {current_visits} · Badges: {current_badges} · Tricks spotted: {current_tricks}"
                    )
                results = send_telegram_to_all(bot_token, chat_id, message)
                telegram_sent = all(v == "sent" for v in results.values())
            else:
                telegram_sent = None

            new_state = {
                "last_visits_total": current_visits,
                "last_checked_at": datetime.now(timezone.utc).isoformat(),
            }
            github_put(STATE_FILE, site_token, new_state, sha, "Visit digest check")

            self._respond(200, {"ok": True, "new_visits": new_visits, "telegram_sent": telegram_sent, "current_visits": current_visits})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
