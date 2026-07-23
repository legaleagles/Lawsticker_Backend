"""
TS LAWCET news watcher — via law.careers360.com, not the government portal.

lawcet.tsche.ac.in explicitly blocks automated access (robots.txt) — this
watches a third-party education news site instead, which both allows
fetching AND tends to report LAWCET updates within hours of the official
announcement (visible from their own "1 day ago" article timestamps).

Only the "TS LAWCET 2026 Latest Update" section is compared between runs,
not the whole page — that page is enormous and full of unrelated content
(download counters, ads, unrelated exam widgets) that changes constantly
and would otherwise trigger false alerts on every single check.

Required environment variables (already set in Vercel):
  SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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
STATE_FILE = "lawcet-news-watch-state.json"
GITHUB_API = "https://api.github.com"
WATCH_URL = "https://law.careers360.com/articles/ts-lawcet-2026"
SECTION_START_MARKERS = ["TS LAWCET 2026 Latest Update", "Latest Update"]
MAX_TELEGRAM_LEN = 3800


def fetch_page_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerWatch/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def html_to_text(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&quot;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_latest_update_section(full_text):
    """
    Finds the "Latest Update" bullet section specifically, and cuts it off
    at the next major heading so unrelated page content never gets included
    in the comparison. Falls back to a fixed-length slice around the marker
    if the exact boundaries can't be found, rather than failing outright.
    """
    for marker in SECTION_START_MARKERS:
        idx = full_text.find(marker)
        if idx != -1:
            # Grab a generous chunk after the marker, then trim at the next
            # heading-like transition (a capitalised multi-word run following
            # a full stop is a reasonable proxy for "next section" on this
            # site's text-stripped output).
            chunk = full_text[idx: idx + 2500]
            cutoff = re.search(r"(TS LAWCET 2026 Exam Date|TS LAWCET 2026 Eligibility)", chunk)
            if cutoff:
                chunk = chunk[:cutoff.start()]
            return chunk.strip()
    return None


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
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": False}).encode()
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
            html = fetch_page_html(WATCH_URL)
            full_text = html_to_text(html)
            section = extract_latest_update_section(full_text)

            if section is None:
                self._respond(200, {"ok": False, "error": "Could not locate the Latest Update section — page structure may have changed."})
                return

            current_hash = hashlib.sha256(section.encode()).hexdigest()
            state, sha = github_get(STATE_FILE, site_token)
            is_first_run = state is None
            previous_hash = state.get("last_hash") if state else None
            changed = is_first_run or (current_hash != previous_hash)

            telegram_sent = None
            if changed:
                snippet = section[:MAX_TELEGRAM_LEN]
                prefix = "🔍 Now watching TS LAWCET news (via Careers360) — baseline:\n\n" if is_first_run else "🔔 TS LAWCET update spotted!\nhttps://law.careers360.com/articles/ts-lawcet-2026\n\n"
                results = send_telegram_to_all(bot_token, chat_id, prefix + snippet)
                telegram_sent = all(v == "sent" for v in results.values())

            new_state = {
                "last_hash": current_hash,
                "last_checked_at": datetime.now(timezone.utc).isoformat(),
                "last_changed_at": datetime.now(timezone.utc).isoformat() if changed else (state.get("last_changed_at") if state else None),
            }
            github_put(STATE_FILE, site_token, new_state, sha, "LAWCET news watch: " + ("change detected" if changed else "no change"))

            self._respond(200, {"ok": True, "changed": changed, "telegram_sent": telegram_sent})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
