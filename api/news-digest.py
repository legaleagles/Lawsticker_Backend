"""
News Digest — fetches Regional (Hyderabad/Telangana), National (India), and
International news via NewsData.io's official API (not scraping — a real,
sanctioned developer API with a commercial-use-friendly free tier), and
writes a clean JSON file the site's news page reads and displays.

Only headline, source, publish time, and a short snippet are stored — never
full article text — respecting copyright and matching how any responsible
news aggregator operates. Every card links back to the original source.

Meant to run every 15-60 minutes via an external scheduler (cron-job.org).
Free tier is 200 API credits/day; at 3 queries per run, hourly (72/day)
fits comfortably, 15-minute (288/day) would exceed it.

Required environment variables (set in Vercel):
  SITE_REPO_TOKEN, NEWSDATA_API_KEY
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
NEWS_FILE = "news-feed.json"
GITHUB_API = "https://api.github.com"
NEWSDATA_BASE = "https://newsdata.io/api/1/latest"

QUERIES = {
    "legal": {"q": "court OR judgment OR verdict OR legislation OR tribunal", "country": "in", "language": "en"},
    "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "en"},
    "national": {"country": "in", "language": "en"},
    "international": {"language": "en", "excludecountry": "in"},
}


def fetch_news(api_key, params):
    q = dict(params)
    q["apikey"] = api_key
    url = NEWSDATA_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerNews/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def extract_articles(api_response, limit=8):
    articles = []
    for item in (api_response.get("results") or [])[:limit]:
        articles.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "source": item.get("source_id", "unknown"),
            "pubDate": item.get("pubDate", ""),
            "image_url": item.get("image_url"),
            "description": (item.get("description") or "")[:180],
            "category": item.get("category") or [],
        })
    return articles


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


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        newsdata_key = os.environ.get("NEWSDATA_API_KEY")

        if not site_token or not newsdata_key:
            self._respond(500, {"ok": False, "error": "Server misconfiguration — missing env vars."})
            return

        try:
            feed = {}
            errors = {}
            for category, params in QUERIES.items():
                try:
                    raw = fetch_news(newsdata_key, params)
                    if raw.get("status") == "success":
                        feed[category] = extract_articles(raw)
                    else:
                        errors[category] = raw.get("results", {}).get("message", "Unknown API error")
                        feed[category] = []
                except urllib.error.HTTPError as e:
                    try:
                        errors[category] = e.read().decode()
                    except Exception:
                        errors[category] = str(e)
                    feed[category] = []
                except Exception as e:
                    errors[category] = str(e)
                    feed[category] = []

            existing, sha = github_get(NEWS_FILE, site_token)
            output = dict(existing) if existing else {}
            output["updated_at"] = datetime.now(timezone.utc).isoformat()
            output["categories"] = feed
            # categories_te / categories_hi (written by the separate multilingual
            # script on its own schedule) are deliberately left untouched here.

            github_put(NEWS_FILE, site_token, output, sha, "News digest update")

            total = sum(len(v) for v in feed.values())
            self._respond(200, {"ok": True, "total_articles": total, "counts": {k: len(v) for k, v in feed.items()}, "errors": errors or None})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
