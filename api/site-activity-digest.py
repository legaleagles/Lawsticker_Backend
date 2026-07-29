"""
Site Activity Digest — combines two previously-separate hourly jobs into
one function, purely to stay under Vercel Hobby's 12-function-per-deployment
cap as the site has grown. Each job's logic is untouched and wrapped in its
own try/except, so a failure in one never affects the other.

1. Visit Digest — reports new site visits since the last check, to Telegram.
2. News Digest — fetches Regional/National/International/Legal news via
   NewsData.io's official API and writes news-feed.json for the site.

Required environment variables (already set in Vercel):
  SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NEWSDATA_API_KEY

IMPORTANT: the external scheduler (cron-job.org) entries that used to call
/api/visit-digest and /api/news-digest separately should now both point to
/api/site-activity-digest instead — this one call does both jobs.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

REPO = "legaleagles/LabourLaw2"
GITHUB_API = "https://api.github.com"

PULSE_FILE = "pulse-counts.json"
VISIT_STATE_FILE = "visit-digest-state.json"

NEWS_FILE = "news-feed.json"
NEWSDATA_BASE = "https://newsdata.io/api/1/latest"
NEWS_QUERIES = {
    "legal": {"q": "court OR judgment OR verdict OR legislation OR tribunal", "country": "in", "language": "en"},
    "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "en"},
    "national": {"country": "in", "language": "en"},
    "international": {"language": "en", "excludecountry": "in"},
}


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
            results[cid] = f"failed: {e}"
    return results


def fetch_news(api_key, params):
    query = dict(params)
    query["apikey"] = api_key
    url = f"{NEWSDATA_BASE}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def extract_articles(api_response, limit=8):
    results = api_response.get("results", [])[:limit]
    articles = []
    for r in results:
        articles.append({
            "title": r.get("title", ""),
            "link": r.get("link", ""),
            "source": r.get("source_id", ""),
            "pubDate": r.get("pubDate", ""),
            "description": (r.get("description") or "")[:200],
        })
    return articles


def run_visit_digest(site_token, bot_token, chat_id):
    try:
        pulse, _ = github_get(PULSE_FILE, site_token)
        current_visits = (pulse or {}).get("visits_total", 0)
        current_badges = (pulse or {}).get("badges_total", 0)
        current_tricks = (pulse or {}).get("tricks_total", 0)

        state, sha = github_get(VISIT_STATE_FILE, site_token)
        is_first_run = state is None
        last_visits = state.get("last_visits_total", 0) if state else 0
        new_visits = current_visits - last_visits

        telegram_sent = None
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

        new_state = {"last_visits_total": current_visits, "last_checked_at": datetime.now(timezone.utc).isoformat()}
        github_put(VISIT_STATE_FILE, site_token, new_state, sha, "Visit digest check")

        return {"ok": True, "new_visits": new_visits, "telegram_sent": telegram_sent, "current_visits": current_visits}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_news_digest(site_token, newsdata_key):
    try:
        existing, sha = github_get(NEWS_FILE, site_token)
        existing = existing or {}
        previous_categories = existing.get("categories", {})

        feed = {}
        errors = {}
        stale = []
        for category, params in NEWS_QUERIES.items():
            try:
                raw = fetch_news(newsdata_key, params)
                if raw.get("status") == "success":
                    articles = extract_articles(raw)
                    if articles:
                        feed[category] = articles
                    else:
                        feed[category] = previous_categories.get(category, [])
                        stale.append(category)
                else:
                    errors[category] = raw.get("results", {}).get("message", "Unknown API error")
                    feed[category] = previous_categories.get(category, [])
                    stale.append(category)
            except urllib.error.HTTPError as e:
                try:
                    errors[category] = e.read().decode()
                except Exception:
                    errors[category] = str(e)
                feed[category] = previous_categories.get(category, [])
                stale.append(category)
            except Exception as e:
                errors[category] = str(e)
                feed[category] = previous_categories.get(category, [])
                stale.append(category)

        output = dict(existing)
        if len(stale) < len(NEWS_QUERIES):
            output["updated_at"] = datetime.now(timezone.utc).isoformat()
        output["categories"] = feed
        github_put(NEWS_FILE, site_token, output, sha, "News digest update")

        total = sum(len(v) for v in feed.values())
        return {"ok": True, "total_articles": total, "counts": {k: len(v) for k, v in feed.items()}, "stale_categories": stale or None, "errors": errors or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        newsdata_key = os.environ.get("NEWSDATA_API_KEY")

        if not site_token:
            self._respond(500, {"ok": False, "error": "Server misconfiguration — missing SITE_REPO_TOKEN."})
            return

        visit_result = run_visit_digest(site_token, bot_token, chat_id) if (bot_token and chat_id) else {"ok": False, "error": "missing telegram env vars"}
        news_result = run_news_digest(site_token, newsdata_key) if newsdata_key else {"ok": False, "error": "missing NEWSDATA_API_KEY"}

        self._respond(200, {"ok": True, "visit_digest": visit_result, "news_digest": news_result})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
