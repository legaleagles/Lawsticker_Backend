"""
Daily Digest — posts petrol, diesel, gold, and silver prices to the
Telegram channel once a day, formatted nicely with day-over-day change
indicators.

Gold/silver figures are read directly from site-config.json (already kept
fresh daily by update-gold-rate.py) rather than re-fetched here, so the
numbers always match what's shown on the live site's own calculators.

Petrol/diesel are fetched from goodreturns.in's Hyderabad-specific pages —
verified fetchable (no robots block), India Oil-sourced daily rates.

Required environment variables (already set in Vercel from earlier setup):
  SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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
CONFIG_FILE = "site-config.json"
STATE_FILE = "daily-digest-state.json"
GITHUB_API = "https://api.github.com"

PETROL_URL = "https://www.goodreturns.in/petrol-price-in-hyderabad.html"
DIESEL_URL = "https://www.goodreturns.in/diesel-price-in-hyderabad.html"


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerDigest/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&#8377;", "₹").replace("&#x20b9;", "₹").replace("&rupee;", "₹")
    text = re.sub(r"&nbsp;|&amp;|&quot;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_fuel_price(text, fuel_word):
    patterns = [
        rf"{fuel_word} price in Hyderabad (?:is at|stands at) (?:₹|Rs\.?)\s*([\d.]+)",
        rf"{fuel_word} price.{{0,30}}?Hyderabad.{{0,30}}?(?:₹|Rs\.?)\s*([\d.]+)",
        rf"(?:₹|Rs\.?)\s*([\d.]+)\s*per litre",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return float(m.group(1))
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
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
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
        except urllib.error.HTTPError as e:
            try:
                results[cid] = e.read().decode()
            except Exception:
                results[cid] = str(e)
        except Exception as e:
            results[cid] = str(e)
    return results


def arrow(current, previous):
    if previous is None:
        return ""
    diff = round(current - previous, 2)
    if diff > 0:
        return f" 🔺 +₹{diff}"
    elif diff < 0:
        return f" 🔻 -₹{abs(diff)}"
    return " ➖ no change"


def build_message(petrol, diesel, gold, silver, prev):
    today = datetime.now(timezone.utc).strftime("%d %B %Y")
    lines = []
    lines.append(f"📊 <b>Today's Rates — {today}</b>")
    lines.append("")
    lines.append(f"⛽ <b>Petrol</b> (Hyderabad): ₹{petrol}/L{arrow(petrol, prev.get('petrol'))}")
    lines.append(f"🛢️ <b>Diesel</b> (Hyderabad): ₹{diesel}/L{arrow(diesel, prev.get('diesel'))}")
    lines.append("")
    lines.append(f"🥇 <b>Gold</b> (24K): ₹{gold}/gram{arrow(gold, prev.get('gold'))}")
    lines.append(f"🥈 <b>Silver</b> (999): ₹{silver}/gram{arrow(silver, prev.get('silver'))}")
    lines.append("")
    lines.append("🔗 More tools: lawsticker-ai.com/calculators.html")
    return "\n".join(lines)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        site_token = os.environ.get("SITE_REPO_TOKEN")
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")

        if not site_token or not bot_token or not chat_id:
            self._respond(500, {"ok": False, "error": "Server misconfiguration."})
            return

        try:
            petrol_text = fetch_text(PETROL_URL)
            diesel_text = fetch_text(DIESEL_URL)
            petrol = extract_fuel_price(petrol_text, "petrol")
            diesel = extract_fuel_price(diesel_text, "diesel")

            if petrol is None or diesel is None:
                self._respond(200, {"ok": False, "error": "Could not extract fuel prices — source page format may have changed.", "petrol": petrol, "diesel": diesel})
                return

            config, _ = github_get(CONFIG_FILE, site_token)
            rates = (config or {}).get("rates", {})
            gold = rates.get("gold_24k_per_gram_inr")
            silver = rates.get("silver_999_per_gram_inr")

            if gold is None or silver is None:
                self._respond(200, {"ok": False, "error": "Gold/silver rates not found in site-config.json."})
                return

            state, sha = github_get(STATE_FILE, site_token)
            prev = state or {}

            message = build_message(petrol, diesel, gold, silver, prev)
            results = send_telegram_to_all(bot_token, chat_id, message)
            telegram_sent = all(v == "sent" for v in results.values())

            new_state = {
                "petrol": petrol, "diesel": diesel, "gold": gold, "silver": silver,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
            github_put(STATE_FILE, site_token, new_state, sha, "Daily digest posted")

            self._respond(200, {"ok": True, "telegram_sent": telegram_sent, "telegram_results": results, "values": new_state})

        except Exception as e:
            self._respond(500, {"ok": False, "error": str(e)})

    def _respond(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
