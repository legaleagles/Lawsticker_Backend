"""
Site Watchers — combines two previously-separate change-detection jobs into
one function, purely to stay under Vercel Hobby's 12-function-per-deployment
cap as the site has grown. Each watcher's logic is untouched and wrapped in
its own try/except, so a failure in one never affects the other.

1. PLC Watch — plchyd.ac.in homepage, whole-page hash comparison.
2. LAWCET News Watch — law.careers360.com's "Latest Update" section only.

Required environment variables (already set in Vercel):
  SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

IMPORTANT: the external scheduler (cron-job.org) entries that used to call
/api/plc-watch and /api/lawcet-news-watch separately should now both point
to /api/site-watchers instead — this one call does both jobs.
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
GITHUB_API = "https://api.github.com"
MAX_TELEGRAM_LEN = 3800

PLC_STATE_FILE = "plc-watch-state.json"
PLC_URL = "https://plchyd.ac.in/"

LAWCET_STATE_FILE = "lawcet-news-watch-state.json"
LAWCET_URL = "https://law.careers360.com/articles/ts-lawcet-2026"
LAWCET_SECTION_MARKERS = ["TS LAWCET 2026 Latest Update", "Latest Update"]


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
    results = {}
    for cid in [c.strip() for c in chat_id_config.split(",") if c.strip()]:
        try:
            send_telegram(bot_token, cid, text)
            results[cid] = "sent"
        except Exception as e:
            results[cid] = f"failed: {e}"
    return results


def html_to_text(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&quot;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerWatch/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return html_to_text(html)


def extract_latest_update_section(full_text):
    """
    Careers360's page opens with a table-of-contents listing every section
    heading, including the real one further down — using the LAST
    occurrence of the marker reliably lands on the actual content section,
    since the ToC always appears before the real sections in reading order.
    """
    for marker in LAWCET_SECTION_MARKERS:
        idx = full_text.rfind(marker)
        if idx != -1:
            chunk = full_text[idx: idx + 2500]
            cutoff = re.search(r"(TS LAWCET 2026 Exam Date|TS LAWCET 2026 Eligibility)", chunk)
            if cutoff:
                chunk = chunk[:cutoff.start()]
            return chunk.strip()
    return None


def run_plc_watch(site_token, bot_token, chat_id):
    try:
        page_text = fetch_page_text(PLC_URL)
        current_hash = hashlib.sha256(page_text.encode()).hexdigest()

        state, sha = github_get(PLC_STATE_FILE, site_token)
        is_first_run = state is None
        previous_hash = state.get("last_hash") if state else None
        changed = is_first_run or (current_hash != previous_hash)

        telegram_sent = None
        if changed:
            snippet = page_text[:MAX_TELEGRAM_LEN]
            prefix = ("🔍 Now watching plchyd.ac.in — baseline snapshot:\n\n" if is_first_run
                      else "🔔 plchyd.ac.in homepage has changed!\nhttps://plchyd.ac.in/\n\n")
            results = send_telegram_to_all(bot_token, chat_id, prefix + snippet)
            telegram_sent = all(v == "sent" for v in results.values())

        new_state = {
            "last_hash": current_hash,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
            "last_changed_at": datetime.now(timezone.utc).isoformat() if changed else (state.get("last_changed_at") if state else None),
        }
        github_put(PLC_STATE_FILE, site_token, new_state, sha, "PLC watch: " + ("change detected" if changed else "no change"))

        return {"ok": True, "changed": changed, "telegram_sent": telegram_sent}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_lawcet_news_watch(site_token, bot_token, chat_id):
    try:
        full_text = fetch_page_text(LAWCET_URL)
        section = extract_latest_update_section(full_text)
        if section is None:
            return {"ok": False, "error": "Could not locate the Latest Update section — page structure may have changed."}

        current_hash = hashlib.sha256(section.encode()).hexdigest()
        state, sha = github_get(LAWCET_STATE_FILE, site_token)
        is_first_run = state is None
        previous_hash = state.get("last_hash") if state else None
        changed = is_first_run or (current_hash != previous_hash)

        telegram_sent = None
        if changed:
            snippet = section[:MAX_TELEGRAM_LEN]
            prefix = ("🔍 Now watching TS LAWCET news (via Careers360) — baseline:\n\n" if is_first_run
                      else "🔔 TS LAWCET update spotted!\nhttps://law.careers360.com/articles/ts-lawcet-2026\n\n")
            results = send_telegram_to_all(bot_token, chat_id, prefix + snippet)
            telegram_sent = all(v == "sent" for v in results.values())

        new_state = {
            "last_hash": current_hash,
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
            "last_changed_at": datetime.now(timezone.utc).isoformat() if changed else (state.get("last_changed_at") if state else None),
        }
        github_put(LAWCET_STATE_FILE, site_token, new_state, sha, "LAWCET news watch: " + ("change detected" if changed else "no change"))

        return {"ok": True, "changed": changed, "telegram_sent": telegram_sent}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

        if not site_token or not bot_token or not chat_id:
            self._respond(500, {"ok": False, "error": "Server misconfiguration — missing env vars."})
            return

        plc_result = run_plc_watch(site_token, bot_token, chat_id)
        lawcet_result = run_lawcet_news_watch(site_token, bot_token, chat_id)

        self._respond(200, {"ok": True, "plc_watch": plc_result, "lawcet_news_watch": lawcet_result})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
