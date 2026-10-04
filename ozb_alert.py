"""OzBargain deal alert.

Watches the OzBargain new-deals RSS feed and notifies you when a deal matches
your watchlist. Two kinds of watches:
  - item:     specific product, matched by keywords (tracks the lowest price seen)
  - category: every deal in an OzBargain category (optionally filtered by keywords/price)

Usage:
  python ozb_alert.py add "Seagate HDD" -k seagate -m 300
  python ozb_alert.py add "Gaming" -c gaming
  python ozb_alert.py add "Cheap SSDs" -c computing -k ssd -m 100
  python ozb_alert.py list
  python ozb_alert.py remove "Gaming"
  python ozb_alert.py check            # check the feed once
  python ozb_alert.py watch -i 10      # keep checking every 10 minutes (local)
  python ozb_alert.py phone my-topic   # set ntfy.sh topic for phone alerts (local)
  python ozb_alert.py test             # send a test notification

Category slugs: computing, electrical-electronics, gaming, mobile, home-garden,
groceries, fashion-apparel, health-beauty, entertainment, travel, automotive,
sports-outdoors, toys-kids, alcohol, dining-takeaway, financial, internet, ...
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

BASE = "https://www.ozbargain.com.au"
FEED_URL = f"{BASE}/deals/feed"
HERE = Path(__file__).resolve().parent
WATCHLIST = HERE / "watchlist.json"
SEEN = HERE / "seen.json"
HISTORY = HERE / "history.json"
STATUS = HERE / "STATUS.md"
TOPIC_FILE = HERE / ".ntfy_topic"
OZB_NS = "{https://www.ozbargain.com.au}"
PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
UA = {"User-Agent": "ozb-alert/1.0 (personal deal alerts)"}
RECENT_KEEP = 15


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


def ntfy_topic():
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"].strip()
    return TOPIC_FILE.read_text().strip() if TOPIC_FILE.exists() else ""


def kind(watch):
    return "item" if watch.get("keywords") else "category"


# ---------- feed ----------

def fetch_deals(url=FEED_URL):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read())

    deals = []
    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        price = parse_price(title)
        try:
            date = parsedate_to_datetime(item.findtext("pubDate", "")).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date = datetime.now().strftime("%Y-%m-%d")
        cats = item.findall("category")
        deals.append({
            "id": item.findtext("guid", "").split(" ")[0],
            "title": title,
            "link": item.findtext("link", ""),
            "date": date,
            "categories": [c.text or "" for c in cats],
            "cat_slugs": [c.get("domain", "").rstrip("/").rsplit("/", 1)[-1] for c in cats],
            "price": price,
        })
    return deals


def parse_price(title):
    """First $ amount that looks like the deal price (skips "$X off", "save $X", "RRP $X", "min spend $X")."""
    for m in PRICE_RE.finditer(title):
        before = title[max(0, m.start() - 12):m.start()].lower().strip()
        after = title[m.end():m.end() + 5].lower().lstrip()
        if after.startswith(("off", "min")) or re.search(r"(save|rrp|spend|over|up to|was)\s*a?$", before):
            continue
        return float(m.group(1).replace(",", ""))
    return None


# ---------- matching ----------

def matches(deal, watch):
    """Category (if set) must match; all keywords must appear; no exclude word; price <= max_price."""
    cat = watch.get("category")
    if cat:
        cat = cat.lower()
        if cat not in deal["cat_slugs"] and cat not in [c.lower() for c in deal["categories"]]:
            return False
    text = deal["title"].lower()
    if not all(k.lower() in text for k in watch.get("keywords", [])):
        return False
    if any(x.lower() in text for x in watch.get("exclude", [])):
        return False
    max_price = watch.get("max_price")
    if max_price is not None and deal["price"] is not None and deal["price"] > max_price:
        return False
    return True


def record(history, watch, deal):
    """Store the deal in this watch's history. Returns (previous lowest, is_new_lowest)."""
    h = history.setdefault(watch["name"], {"lowest": None, "recent": []})
    entry = {k: deal[k] for k in ("title", "link", "price", "date")}
    if all(r["link"] != deal["link"] for r in h["recent"]):
        h["recent"] = ([entry] + h["recent"])[:RECENT_KEEP]
    prev = h["lowest"]
    is_new_low = deal["price"] is not None and (prev is None or deal["price"] < prev["price"])
    if is_new_low:
        h["lowest"] = entry
    return prev, is_new_low


def backfill(history, watch):
    """Seed price history from OzBargain tag feeds (last ~10 deals per tag), without notifying."""
    tags = watch.get("tags") or [re.sub(r"[^a-z0-9]+", "-", k.lower()).strip("-") for k in watch.get("keywords", [])]
    found = 0
    for tag in tags:
        try:
            deals = fetch_deals(f"{BASE}/tag/{tag}/feed")
        except Exception:
            continue
        for deal in deals:
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


def notify_ntfy(topic, title, body, url, tags):
    payload = {"topic": topic, "title": title, "message": body, "click": url, "tags": tags}
    req = urllib.request.Request("https://ntfy.sh", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ntfy failed: {e}")


def notify(title, body, url, tags=("moneybag",)):
    if sys.platform == "win32":
        notify_windows(title, body, url)
    topic = ntfy_topic()
    if topic:
        notify_ntfy(topic, title, body, url, list(tags))


def alert(watch, deal, prev, is_new_low):
    if kind(watch) == "item":
        if is_new_low and prev is not None:
            title, tags = f"NEW LOWEST · {watch['name']} {fmt(deal['price'])}", ["fire"]
        else:
            title, tags = f"{watch['name']} {fmt(deal['price'])}", ["moneybag"]
        low = deal if is_new_low else prev
        body = f"{deal['title']}\nLowest seen: {fmt(low['price'])} ({low['date']})" if low else deal["title"]
    else:
        title, tags = f"[{watch['name']}] {fmt(deal['price'])}", ["label"]
        body = deal["title"]
    notify(title, body, deal["link"], tags)


# ---------- status page ----------

def write_status(config, history):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["# OzBargain watchlist", "", f"_Last checked: {now} (UTC)_", ""]

    items = [w for w in config["items"] if kind(w) == "item"]
    cats = [w for w in config["items"] if kind(w) == "category"]

    lines += ["## Specific items", ""]
    if items:
        lines += ["| Item | Lowest seen | Latest deal |", "|---|---|---|"]
        for w in items:
            h = history.get(w["name"], {})
            low, recent = h.get("lowest"), h.get("recent", [])
            low_s = f"**{fmt(low['price'])}** ({low['date']}) [link]({low['link']})" if low else "—"
            last_s = f"{fmt(recent[0]['price'])} ({recent[0]['date']}) [link]({recent[0]['link']})" if recent else "—"
            lines.append(f"| {w['name']} | {low_s} | {last_s} |")
    else:
        lines.append("_None yet._")
    lines.append("")

    lines += ["## Categories", ""]
    if not cats:
        lines += ["_None yet._", ""]
    for w in cats:
        lines += [f"### {w['name']} (`{w['category']}`)", ""]
        recent = history.get(w["name"], {}).get("recent", [])
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
                prev, is_new_low = record(history, watch, deal)
                print(f"  MATCH [{watch['name']}] {deal['title']}\n    {deal['link']}")
                alert(watch, deal, prev, is_new_low)

    save_json(SEEN, sorted(seen, key=lambda s: int(s) if s.isdigit() else 0)[-2000:])
    save_json(HISTORY, history)
    write_status(config, history)
    print(f"[{time.strftime('%H:%M')}] Checked {len(deals)} deals, {hits} new match(es).")


def cmd_watch(args):
    print(f"Watching OzBargain every {args.interval} min. Ctrl+C to stop.")
    while True:
        cmd_check()
        time.sleep(args.interval * 60)


def cmd_add(args):
    if not args.keywords and not args.category:
        args.keywords = args.name.split()
    sync_pull()
    config = load_watchlist()
    config["items"] = [w for w in config["items"] if w["name"].lower() != args.name.lower()]
    watch = {"name": args.name}
    if args.category:
        watch["category"] = args.category.lower()
    watch["keywords"] = args.keywords or []
    watch["exclude"] = args.exclude or []
    watch["max_price"] = args.max_price
    if args.tags:
        watch["tags"] = args.tags
    config["items"].append(watch)
    save_json(WATCHLIST, config)
    print(f"Added {kind(watch)} '{args.name}'.")

    if kind(watch) == "item":
        history = load_json(HISTORY, {})
        history.pop(args.name, None)
        n = backfill(history, watch)
        save_json(HISTORY, history)
        low = history.get(args.name, {}).get("lowest")
        if low:
            print(f"Found {n} recent past deal(s). Lowest: {fmt(low['price'])} ({low['date']}) - {low['title']}")
        else:
            print("No past deals found yet; lowest price will be tracked from now on.")
    sync_push(f"Watch {args.name}")


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
    for label, k in (("Specific items", "item"), ("Categories", "category")):
        print(f"{label}:")
        group = [w for w in config["items"] if kind(w) == k]
        if not group:
            print("  (none)")
        for w in group:
            parts = []
            if w.get("category"):
                parts.append(f"category {w['category']}")
            if w.get("keywords"):
                parts.append(f"keywords [{', '.join(w['keywords'])}]")
            if w.get("max_price") is not None:
                parts.append(f"max {fmt(w['max_price'])}")
            if w.get("exclude"):
                parts.append(f"exclude [{', '.join(w['exclude'])}]")
            low = history.get(w["name"], {}).get("lowest")
            if k == "item":
                parts.append(f"lowest {fmt(low['price'])} ({low['date']})" if low else "lowest —")
            print(f"  - {w['name']}: {' | '.join(parts)}")
    print(f"Phone (ntfy) topic: {ntfy_topic() or '(not set)'}")


def cmd_phone(args):
    if args.topic:
        TOPIC_FILE.write_text(args.topic)
        print(f"Phone alerts (local runs) will go to ntfy topic '{args.topic}'.")
    else:
        TOPIC_FILE.unlink(missing_ok=True)
        print("Phone alerts disabled for local runs.")


def cmd_test(_args):
    notify("OzBargain alert test", "Notifications are working!", BASE)
    print("Test notification sent.")


def main():
    p = argparse.ArgumentParser(description="OzBargain deal alerts")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add or replace a watch")
    a.add_argument("name")
    a.add_argument("-c", "--category", help="OzBargain category slug, e.g. gaming, computing")
    a.add_argument("-k", "--keywords", nargs="+", help="words that must ALL appear (default for items: words of the name)")
    a.add_argument("-x", "--exclude", nargs="+", help="skip deals containing any of these words")
    a.add_argument("-m", "--max-price", type=float, help="only alert at or below this price")
    a.add_argument("-t", "--tags", nargs="+", help="OzBargain tags to look up past prices (default: keywords)")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="remove a watch")
    r.add_argument("name")
    r.set_defaults(func=cmd_remove)

    sub.add_parser("list", help="show watchlist and lowest prices").set_defaults(func=cmd_list)
    sub.add_parser("check", help="check the feed once").set_defaults(func=cmd_check)
    sub.add_parser("test", help="send a test notification").set_defaults(func=cmd_test)

    w = sub.add_parser("watch", help="check repeatedly (local)")
    w.add_argument("-i", "--interval", type=float, default=10, help="minutes between checks")
    w.set_defaults(func=cmd_watch)

    ph = sub.add_parser("phone", help="set ntfy.sh topic for local runs ('' to disable)")
    ph.add_argument("topic")
    ph.set_defaults(func=cmd_phone)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
