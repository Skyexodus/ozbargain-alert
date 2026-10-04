"""OzBargain deal alert.

Watches the OzBargain new-deals RSS feed and notifies you when a deal matches
your watchlist. Two kinds of watches:
  - item:  a specific product; tracks the lowest price seen
  - group: a topic or category (e.g. all SSD deals); lists recent deals

Each watch has "match" terms; a deal matches if ANY term matches:
  - an OzBargain page path:  tag/whisky, cat/gaming, brand/seagate, product/adidas-ultraboost
  - a word/phrase in the deal title (whole words, case-insensitive): "hard drive"
  - words joined by "+" must ALL appear:  "turtle wax+ceramic"
Optional filters: "exclude" (same syntax, any hit skips the deal) and "max_price".

Usage:
  python ozb_alert.py add "Ultraboost" --item -a product/adidas-ultraboost ultraboost
  python ozb_alert.py add "SSD" -a tag/ssd ssd nvme -m 150
  python ozb_alert.py add "Games" -a cat/gaming
  python ozb_alert.py add "NBN" -a tag/nbn nbn -x cat/mobile sim esim
  python ozb_alert.py list
  python ozb_alert.py remove "Games"
  python ozb_alert.py check            # check the feed once
  python ozb_alert.py watch -i 10      # keep checking every 10 minutes (local)
  python ozb_alert.py telegram TOKEN   # connect your Telegram bot for alerts
  python ozb_alert.py test             # send a test notification
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from html import unescape
from xml.sax.saxutils import escape

BASE = "https://www.ozbargain.com.au"
FEED_URL = f"{BASE}/deals/feed"
HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "watchlist.json"
SEEN = HERE / "seen.json"
HISTORY = HERE / "history.json"
STATUS = HERE / "STATUS.md"
TELEGRAM_FILE = HERE / ".telegram"
OZB_NS = "{https://www.ozbargain.com.au}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"
PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
PATH_RE = re.compile(r"^(tag|cat|brand|product|event|store)/[\w-]+$")
UA = {"User-Agent": "ozb-alert/1.0 (personal deal alerts)"}
RECENT_KEEP = 15
HISTORY_YEARS = 2


# ---------- storage ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_watchlist():
    return load_json(WATCHLIST, {"items": []})


def is_item(watch):
    return watch.get("type") == "item"


# ---------- feed ----------

def parse_price(title):
    """First $ amount that looks like the deal price (skips "$X off", "save $X", "RRP $X", "min spend $X")."""
    for m in PRICE_RE.finditer(title):
        before = title[max(0, m.start() - 12):m.start()].lower().strip()
        after = title[m.end():m.end() + 5].lower().lstrip()
        if after.startswith(("off", "min")) or re.search(r"(save|rrp|spend|over|up to|was)\s*a?$", before):
            continue
        return float(m.group(1).replace(",", ""))
    return None


def fetch_deals(url=FEED_URL, retries=2):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                root = ET.fromstring(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(20 * (attempt + 1))
                continue
            raise

    deals = []
    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        try:
            date = parsedate_to_datetime(item.findtext("pubDate", "")).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date = datetime.now().strftime("%Y-%m-%d")
        cats = item.findall("category")
        meta, thumb = item.find(f"{OZB_NS}meta"), item.find(f"{MEDIA_NS}thumbnail")
        image = (meta.get("image") if meta is not None else None) or (thumb.get("url") if thumb is not None else None)
        deals.append({
            "id": item.findtext("guid", "").split(" ")[0],
            "title": title,
            "link": item.findtext("link", ""),
            "date": date,
            "categories": [c.text or "" for c in cats],
            "paths": [c.get("domain", "").replace(BASE, "").strip("/") for c in cats],
            "price": parse_price(title),
            "image": image,
        })
    return deals


# ---------- matching ----------

def has_words(text, phrase):
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text) is not None


def term_hits(deal, term):
    term = term.strip().lower()
    if PATH_RE.match(term):
        return term in deal["paths"]
    title = deal["title"].lower()
    return all(has_words(title, part.strip()) for part in term.split("+"))


def matches(deal, watch):
    if not any(term_hits(deal, t) for t in watch.get("match", [])):
        return False
    if any(term_hits(deal, t) for t in watch.get("exclude", [])):
        return False
    max_price = watch.get("max_price")
    if max_price is not None and deal["price"] is not None and deal["price"] > max_price:
        return False
    return True


def cutoff():
    return (datetime.now() - timedelta(days=365 * HISTORY_YEARS)).strftime("%Y-%m-%d")


def stats(h):
    """Lowest price within the last HISTORY_YEARS years, and the 2 most recent deals."""
    priced = [d for d in h.get("deals", []) if d["price"] is not None and d["date"] >= cutoff()]
    return {"lowest": min(priced, key=lambda d: d["price"]) if priced else None, "last": h.get("deals", [])[:2]}


def record(history, watch, deal):
    """Store the deal in this watch's history. Returns the stats from *before* this deal."""
    h = history.setdefault(watch["name"], {"deals": []})
    before = stats(h)
    if all(d["link"] != deal["link"] for d in h["deals"]):
        entry = {k: deal.get(k) for k in ("title", "link", "price", "date", "image")}
        deals = sorted([entry] + h["deals"], key=lambda d: d["date"], reverse=True)
        h["deals"] = [d for d in deals if d["date"] >= cutoff()] if is_item(watch) else deals[:RECENT_KEEP]
    return before


def fetch_page(url, retries=2):
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(20 * (attempt + 1))
                continue
            raise


def fetch_history(path, max_pages=8):
    """Deals listed on an OzBargain tag/product page, paging back until HISTORY_YEARS ago."""
    deals, oldest = [], cutoff()
    for page in range(max_pages):
        if page:
            time.sleep(6)  # be polite to OzBargain
        html = fetch_page(f"{BASE}/{path}" + (f"?page={page}" if page else ""))
        blocks = html.split('<div class="node node-ozbdeal')[1:]
        for b in blocks:
            nid = re.search(r'id="node(\d+)"', b)
            title = re.search(r'data-title="([^"]*)"', b)
            when = re.search(r"\bon (\d\d)/(\d\d)/(\d{4})", b)
            if not (nid and title and when):
                continue
            title = unescape(title.group(1))
            img = re.search(r'class="foxshot-container">.*?<img src="([^"]+)"', b, re.S)
            deals.append({
                "id": nid.group(1), "title": title, "link": f"{BASE}/node/{nid.group(1)}",
                "date": f"{when.group(3)}-{when.group(2)}-{when.group(1)}",
                "categories": [], "paths": re.findall(r'href="/((?:cat|tag|brand|product)/[\w-]+)"', b) + [path],
                "price": parse_price(title),
                "image": unescape(img.group(1)) if img else None,
            })
        if not blocks or f"page={page + 1}" not in html or (deals and deals[-1]["date"] < oldest):
            break
    return deals


def backfill(history, watch):
    """Seed history without notifying: items get up to HISTORY_YEARS of deals from their
    tag/product pages; groups get the last ~10 deals from each page's RSS feed."""
    paths = [t.lower() for t in watch.get("match", []) if PATH_RE.match(t.lower())]
    if not paths:  # text-only watch: try each word as a tag
        paths = [f"tag/{re.sub(r'[^a-z0-9]+', '-', t.lower()).strip('-')}" for t in watch["match"]]
    found = 0
    for i, path in enumerate(paths):
        if i:
            time.sleep(6)  # be polite to OzBargain
        try:
            deals = fetch_history(path) if is_item(watch) else fetch_deals(f"{BASE}/{path}/feed")
        except Exception as e:
            print(f"  (couldn't read {path}: {e})")
            continue
        for deal in deals:
            deal["paths"].append(path)  # it came from this page's feed, even if the item doesn't list it
            if matches(deal, watch):
                record(history, watch, deal)
                found += 1
    return found


# ---------- notifications ----------

def fmt(price):
    return f"${price:,.2f}" if price is not None else "price n/a"


def notify_windows(title, body, url):
    toast = (
        f'<toast activationType="protocol" launch="{escape(url)}">'
        f'<visual><binding template="ToastGeneric">'
        f'<text>{escape(title)}</text><text>{escape(body)}</text>'
        f'</binding></visual></toast>'
    )
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
{toast}
'@)
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def telegram_config():
    """(bot token, chat id) from env vars (cloud) or the local .telegram file."""
    token, chat = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat) and TELEGRAM_FILE.exists():
        token, chat = load_json(TELEGRAM_FILE, {}).get("token", ""), load_json(TELEGRAM_FILE, {}).get("chat_id", "")
    return token, str(chat)


def telegram_api(token, method, payload=None):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def notify_telegram(token, chat, title, body, url, tags, image=None):
    icon = {"fire": "🔥", "moneybag": "💰", "label": "🏷️"}.get(tags[0] if tags else "", "")
    body = body if len(body) < 850 else body[:850] + "…"  # photo captions max out at 1024 chars
    text = f"{icon} <b>{escape(title)}</b>\n{escape(body)}\n\n<a href=\"{escape(url)}\">Open deal</a>"
    if image:
        try:
            telegram_api(token, "sendPhoto", {"chat_id": chat, "photo": image, "caption": text, "parse_mode": "HTML"})
            return
        except Exception as e:
            print(f"  telegram photo failed ({e}), sending text only")
    try:
        telegram_api(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                            "disable_web_page_preview": False})
    except Exception as e:
        print(f"  telegram failed: {e}")


def notify(title, body, url, tags=("moneybag",), image=None):
    if sys.platform == "win32" and not os.environ.get("CI"):
        notify_windows(title, body, url)
    token, chat = telegram_config()
    if token and chat:
        notify_telegram(token, chat, title, body, url, list(tags), image)


def short(d):
    return f"{fmt(d['price'])} ({d['date']})"


def alert(watch, deal, before):
    if is_item(watch):
        low = before["lowest"]
        if low and deal["price"] is not None and deal["price"] < low["price"]:
            title, tags = f"NEW {HISTORY_YEARS}-YR LOW · {watch['name']} {fmt(deal['price'])}", ["fire"]
        else:
            title, tags = f"{watch['name']} {fmt(deal['price'])}", ["moneybag"]
        body = deal["title"]
        if low:
            body += f"\n{HISTORY_YEARS}-yr low: {short(low)}"
        if before["last"]:
            body += "\nLast prices: " + ", ".join(short(d) for d in before["last"])
    else:
        title, tags = f"[{watch['name']}] {fmt(deal['price'])}", ["label"]
        body = deal["title"]
    notify(title, body, deal["link"], tags, deal.get("image"))


# ---------- status page ----------

def write_status(config, history):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["# OzBargain watchlist", "", f"_Last checked: {now} (UTC)_", ""]

    items = [w for w in config["items"] if is_item(w)]
    groups = [w for w in config["items"] if not is_item(w)]

    lines += ["## Specific items", ""]
    if items:
        lines += [f"| Item | {HISTORY_YEARS}-yr lowest | Latest price | Previous price | Deals ({HISTORY_YEARS} yrs) |",
                  "|---|---|---|---|---|"]
        for w in items:
            h = history.get(w["name"], {})
            s = stats(h)
            cell = lambda d: f"{short(d)} [link]({d['link']})" if d else "—"
            last = s["last"] + [None, None]
            low_s = f"**{cell(s['lowest'])}**" if s["lowest"] else "—"
            lines.append(f"| {w['name']} | {low_s} | {cell(last[0])} | {cell(last[1])} | {len(h.get('deals', []))} |")
    else:
        lines.append("_None yet._")
    lines.append("")

    lines += ["## Groups", ""]
    if not groups:
        lines += ["_None yet._", ""]
    for w in groups:
        lines += [f"### {w['name']}", ""]
        recent = history.get(w["name"], {}).get("deals", [])
        lines += [f"- {d['date']} · {fmt(d['price'])} · [{d['title']}]({d['link']})" for d in recent[:10]] or ["_No deals yet._"]
        lines.append("")

    STATUS.write_text("\n".join(lines), encoding="utf-8")


# ---------- git sync (so local watchlist edits reach the cloud) ----------

def git(*args):
    return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True)


def has_remote():
    return (HERE / ".git").exists() and bool(git("remote").stdout.strip())


def sync_pull():
    if has_remote():
        git("pull", "--rebase", "--autostash", "-q")


def sync_push(msg):
    if has_remote():
        git("add", "watchlist.json", "history.json")
        git("commit", "-q", "-m", msg)
        r = git("push", "-q")
        print("Synced to GitHub." if r.returncode == 0 else f"Push failed: {r.stderr.strip()}")


# ---------- commands ----------

def cmd_check(_args=None):
    config = load_watchlist()
    history = load_json(HISTORY, {})
    seen = set(load_json(SEEN, []))
    try:
        deals = fetch_deals()
    except Exception as e:
        print(f"[{time.strftime('%H:%M')}] Could not fetch feed: {e}")
        return

    hits = 0
    for deal in reversed(deals):  # oldest first
        if deal["id"] in seen:
            continue
        seen.add(deal["id"])
        for watch in config["items"]:
            if matches(deal, watch):
                hits += 1
                before = record(history, watch, deal)
                print(f"  MATCH [{watch['name']}] {deal['title']}\n    {deal['link']}")
                alert(watch, deal, before)

    save_json(SEEN, sorted(seen, key=lambda s: int(s) if s.isdigit() else 0)[-2000:])
    save_json(HISTORY, history)
    write_status(config, history)
    print(f"[{time.strftime('%H:%M')}] Checked {len(deals)} deals, {hits} new match(es).")


def cmd_watch(args):
    print(f"Watching OzBargain every {args.interval} min. Ctrl+C to stop.")
    while True:
        cmd_check()
        time.sleep(args.interval * 60)


def add_watch(config, history, watch):
    config["items"] = [w for w in config["items"] if w["name"].lower() != watch["name"].lower()]
    config["items"].append(watch)
    history.pop(watch["name"], None)
    backfill(history, watch)
    h = history.get(watch["name"], {})
    s, n = stats(h), len(h.get("deals", []))
    if is_item(watch) and s["lowest"]:
        last = ", ".join(short(d) for d in s["last"])
        print(f"Added item '{watch['name']}': {n} deal(s) in {HISTORY_YEARS} yrs, lowest {short(s['lowest'])}, last: {last}")
    else:
        print(f"Added {watch.get('type', 'group')} '{watch['name']}': {n} past deal(s) found.")


def cmd_add(args):
    sync_pull()
    config, history = load_watchlist(), load_json(HISTORY, {})
    watch = {"name": args.name, "type": "item" if args.item else "group",
             "match": args.any or [args.name], "exclude": args.exclude or [], "max_price": args.max_price}
    add_watch(config, history, watch)
    save_json(WATCHLIST, config)
    save_json(HISTORY, history)
    sync_push(f"Watch {args.name}")


def cmd_import(args):
    """Replace/add watches from a JSON file: {"items": [ {name, type, match, exclude, max_price}, ... ]}"""
    sync_pull()
    config, history = load_watchlist(), load_json(HISTORY, {})
    for i, watch in enumerate(load_json(Path(args.file), {"items": []})["items"]):
        if i:
            time.sleep(6)
        add_watch(config, history, watch)
    save_json(WATCHLIST, config)
    save_json(HISTORY, history)
    write_status(config, history)
    sync_push("Import watchlist")


def cmd_remove(args):
    sync_pull()
    config = load_watchlist()
    before = len(config["items"])
    config["items"] = [w for w in config["items"] if w["name"].lower() != args.name.lower()]
    if len(config["items"]) == before:
        print(f"'{args.name}' not found.")
        return
    save_json(WATCHLIST, config)
    history = load_json(HISTORY, {})
    history.pop(args.name, None)
    save_json(HISTORY, history)
    print("Removed.")
    sync_push(f"Unwatch {args.name}")


def cmd_list(_args):
    sync_pull()
    config = load_watchlist()
    history = load_json(HISTORY, {})
    for label, want_item in (("Specific items", True), ("Groups", False)):
        print(f"{label}:")
        group = [w for w in config["items"] if is_item(w) == want_item]
        if not group:
            print("  (none)")
        for w in group:
            parts = [f"match [{', '.join(w.get('match', []))}]"]
            if w.get("exclude"):
                parts.append(f"exclude [{', '.join(w['exclude'])}]")
            if w.get("max_price") is not None:
                parts.append(f"max {fmt(w['max_price'])}")
            h = history.get(w["name"], {})
            if want_item:
                s = stats(h)
                parts.append(f"{HISTORY_YEARS}-yr low {short(s['lowest'])}" if s["lowest"] else f"{HISTORY_YEARS}-yr low —")
                if s["last"]:
                    parts.append("last " + ", ".join(short(d) for d in s["last"]))
            else:
                parts.append(f"{len(h.get('deals', []))} recent deal(s)")
            print(f"  - {w['name']}: {' | '.join(parts)}")
    print(f"Telegram: {'connected' if all(telegram_config()) else 'not set (run: python ozb_alert.py telegram TOKEN)'}")


def cmd_telegram(args):
    """Find your chat with the bot, save it locally and as GitHub secrets, and send a test message."""
    token = args.token.strip()
    try:
        me = telegram_api(token, "getMe")["result"]
        updates = telegram_api(token, "getUpdates")["result"]
    except Exception as e:
        print(f"That bot token didn't work ({e}). Copy it again from BotFather.")
        return
    chats = [u["message"]["chat"] for u in updates if "message" in u]
    if not chats:
        print(f"Open Telegram, search for @{me['username']}, press Start (or send any message), then run this again.")
        return
    chat = chats[-1]
    save_json(TELEGRAM_FILE, {"token": token, "chat_id": chat["id"]})
    for name, value in (("TELEGRAM_TOKEN", token), ("TELEGRAM_CHAT_ID", str(chat["id"]))):
        r = subprocess.run(["gh", "secret", "set", name, "--body", value], cwd=HERE, capture_output=True, text=True)
        if r.returncode:
            print(f"Couldn't save {name} to GitHub: {r.stderr.strip()}")
    notify_telegram(token, chat["id"], "OzBargain alerts connected", f"Hi {chat.get('first_name', '')}! Deals will arrive here.",
                    "https://www.ozbargain.com.au", ["moneybag"])
    print(f"Connected to @{me['username']} (chat {chat['id']}). Check Telegram for a test message.")


def cmd_test(_args):
    notify("OzBargain alert test", "Notifications are working!", BASE)
    print("Test notification sent.")


def main():
    p = argparse.ArgumentParser(description="OzBargain deal alerts")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add or replace a watch")
    a.add_argument("name")
    a.add_argument("-a", "--any", nargs="+", help="match terms: tag/x, cat/x, product/x, words, or a+b (default: the name)")
    a.add_argument("-x", "--exclude", nargs="+", help="skip deals matching any of these terms")
    a.add_argument("-m", "--max-price", type=float, help="only alert at or below this price")
    a.add_argument("--item", action="store_true", help="specific product: track lowest price")
    a.set_defaults(func=cmd_add)

    im = sub.add_parser("import", help="add watches from a JSON file")
    im.add_argument("file")
    im.set_defaults(func=cmd_import)

    r = sub.add_parser("remove", help="remove a watch")
    r.add_argument("name")
    r.set_defaults(func=cmd_remove)

    sub.add_parser("list", help="show watchlist and lowest prices").set_defaults(func=cmd_list)
    sub.add_parser("check", help="check the feed once").set_defaults(func=cmd_check)
    sub.add_parser("test", help="send a test notification").set_defaults(func=cmd_test)

    w = sub.add_parser("watch", help="check repeatedly (local)")
    w.add_argument("-i", "--interval", type=float, default=10, help="minutes between checks")
    w.set_defaults(func=cmd_watch)

    tg = sub.add_parser("telegram", help="connect a Telegram bot (token from @BotFather)")
    tg.add_argument("token")
    tg.set_defaults(func=cmd_telegram)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
