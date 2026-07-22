"""
Daily gold & silver rate fetcher — runs on a schedule via Vercel Cron.

Mirrors the exact formula already used by admin-config.html's manual
"Auto-fetch" button: international spot price (USD/troy oz) converted to
INR/gram, then adjusted by the site's configured India premium percentage.
This script only replaces the *fetching*, not the premium logic itself —
the premium percentages remain whatever is currently set in site-config.json,
so a manual adjustment there still takes effect on the next automated run.

Required environment variable (set in Vercel dashboard):
  SITE_REPO_TOKEN  - a fine-grained GitHub PAT scoped ONLY to
                      legaleagles/LabourLaw2, with Contents: Read and write.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import urllib.request
from datetime import datetime, timezone

REPO = "legaleagles/LabourLaw2"
CONFIG_FILE = "site-config.json"
GITHUB_API = "https://api.github.com"
GRAMS_PER_TROY_OZ = 31.1034768


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lawsticker-ai-cron/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def github_get(path, token):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
        content = base64.b64decode(data["content"]).decode()
        return json.loads(content), data["sha"]


def github_put(path, token, content_obj, sha, message):
    body = json.dumps(content_obj, indent=2, ensure_ascii=False).encode()
    payload = {
        "message": message,
        "content": base64.b64encode(body).decode(),
        "branch": "main",
        "sha": sha,
    }
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


def compute_rate(usd_per_oz, usd_to_inr, premium_pct):
    spot_inr_per_gram = (usd_per_oz / GRAMS_PER_TROY_OZ) * usd_to_inr
    adjusted = spot_inr_per_gram * (1 + premium_pct / 100)
    return round(adjusted)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        token = os.environ.get("SITE_REPO_TOKEN")
        if not token:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            gold = fetch_json("https://api.gold-api.com/price/XAU")
            silver = fetch_json("https://api.gold-api.com/price/XAG")
            fx = fetch_json("https://open.er-api.com/v6/latest/USD")
            usd_to_inr = fx["rates"]["INR"]

            config, sha = github_get(CONFIG_FILE, token)
            rates = config.setdefault("rates", {})

            gold_premium = rates.get("gold_india_premium_pct", 15)
            silver_premium = rates.get("silver_india_premium_pct", 32)

            new_gold = compute_rate(gold["price"], usd_to_inr, gold_premium)
            new_silver = compute_rate(silver["price"], usd_to_inr, silver_premium)

            rates["gold_24k_per_gram_inr"] = new_gold
            rates["silver_999_per_gram_inr"] = new_silver
            rates["updated_at"] = datetime.now(timezone.utc).isoformat()
            rates["updated_by"] = "auto-cron-daily"

            github_put(CONFIG_FILE, token, config, sha, "Daily automated gold/silver rate update")

            self._respond(200, {
                "ok": True,
                "gold_24k_per_gram_inr": new_gold,
                "silver_999_per_gram_inr": new_silver,
            })
        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
