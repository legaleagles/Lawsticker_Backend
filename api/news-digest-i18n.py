"""
Multilingual news fetch — Telugu and Hindi, Regional + National only.

Scoped deliberately narrower than the English fetch:
  - Only Regional and National, not Legal or International. Native-language
    coverage for niche legal/court reporting and world news is genuinely
    thin in Telugu/Hindi sources — forcing those categories would mean
    showing mostly-empty results, which is worse than clearly falling back
    to English on the site.
  - Runs every 3 hours (via external scheduler), not hourly like English,
    to stay within NewsData.io's 200 free-credit/day budget alongside the
    English fetch and everything else already using that same quota.

Writes to categories_te / categories_hi keys in news-feed.json, preserving
the categories (English) key written by the separate hourly script —
each script reads-merges-writes rather than overwriting the whole file.

Required environment variables (already set in Vercel):
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

LANGUAGES = {
    "te": {
        "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "te"},
        "national": {"country": "in", "language": "te"},
    },
    "hi": {
        "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "hi"},
        "national": {"country": "in", "language": "hi"},
    },
}


def fetch_news(api_key, params):
    q = dict(params)
    q["apikey"] = api_key
    url = NEWSDATA_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerNews/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def extract_articles(api_response, limit=6):
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
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            results = {}
            errors = {}
            for lang, queries in LANGUAGES.items():
                results[lang] = {}
                for category, params in queries.items():
                    key = f"{lang}_{category}"
                    try:
                        raw = fetch_news(newsdata_key, params)
                        if raw.get("status") == "success":
                            results[lang][category] = extract_articles(raw)
                        else:
                            errors[key] = raw.get("results", {}).get("message", "Unknown API error")
                            results[lang][category] = []
                    except urllib.error.HTTPError as e:
                        try:
                            errors[key] = e.read().decode()
                        except Exception:
                            errors[key] = str(e)
                        results[lang][category] = []
                    except Exception as e:
                        errors[key] = str(e)
                        results[lang][category] = []

            existing, sha = github_get(NEWS_FILE, site_token)
            output = dict(existing) if existing else {}
            output["updated_at_i18n"] = datetime.now(timezone.utc).isoformat()
            output["categories_te"] = results.get("te", {})
            output["categories_hi"] = results.get("hi", {})
            # categories (English, written by the separate hourly script)
            # is deliberately left untouched here.

            github_put(NEWS_FILE, site_token, output, sha, "Multilingual news update (te/hi)")

            total = sum(len(v) for lang_data in results.values() for v in lang_data.values())
            self._respond(200, {
                "ok": True,
                "total_articles": total,
                "counts": {f"{lang}_{cat}": len(arts) for lang, cats in results.items() for cat, arts in cats.items()},
                "errors": errors or None,
            })

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
