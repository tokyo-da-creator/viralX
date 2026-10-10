#!/usr/bin/env python3
"""
PokeRadar — get Pokémon TCG stories before the big X accounts do.

Polls news / Reddit / RSS feeds, scores every new story against a
"will this go viral" filter, and pushes the good ones to your phone
(ntfy) and/or Discord. Run it on a schedule (GitHub Actions) or locally
with --loop.

Usage:
  python radar.py                 # one scan (what the scheduler runs)
  python radar.py --dry-run       # print scored stories, send nothing, save nothing
  python radar.py --loop 120      # scan every 2 minutes (run on your own machine)
  python radar.py --test-notify   # send a test push to check your setup

Env vars:
  NTFY_TOPIC            your private ntfy topic name (phone push)
  NTFY_SERVER           optional, defaults to https://ntfy.sh
  NTFY_TOKEN            optional, if your ntfy server needs auth
  DISCORD_WEBHOOK_URL   optional Discord channel webhook
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PokeRadar/1.0 (news monitor)"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
STOPWORDS = {
    "the", "and", "for", "with", "from", "after", "before", "over", "into", "this",
    "that", "are", "was", "were", "its", "his", "her", "their", "has", "have", "had",
    "new", "says", "said", "pokemon", "card", "cards", "tcg", "who", "what", "how",
    "why", "out", "off", "about", "more", "than", "just", "now", "get", "gets",
}

# ---------------------------------------------------------------- helpers

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def fold(text: str) -> str:
    """Lowercase + normalise width, and treat 'pokémon' as 'pokemon'. Keeps Japanese intact."""
    return unicodedata.normalize("NFKC", text or "").lower().replace("é", "e")


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def domain_of(url: str) -> str:
    try:
        d = urlparse(url or "").netloc.lower()
    except Exception:
        return ""
    return d[4:] if d.startswith("www.") else d


def fmt_age(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m}m ago" if m < 60 else f"{m // 60}h{m % 60:02d}m ago"

# ---------------------------------------------------------------- feeds

def build_feeds(cfg: dict) -> list[dict]:
    feeds = []
    gn = cfg.get("google_news", {})
    for q in gn.get("en", []):
        feeds.append({
            "name": f"Google News: {q}", "kind": "gnews",
            "url": f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&hl=en-US&gl=US&ceid=US:en",
        })
    for q in gn.get("ja", []):
        feeds.append({
            "name": f"Google News JP: {q}", "kind": "gnews",
            "url": f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&hl=ja&gl=JP&ceid=JP:ja",
        })
    for q in cfg.get("bing_news", []):
        feeds.append({
            "name": f"Bing News: {q}", "kind": "bing",
            "url": f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss",
        })
    for sub in cfg.get("reddit", []):
        feeds.append({
            "name": f"r/{sub}", "kind": "reddit", "tcg_context": True,
            "weight": cfg.get("reddit_weight", -1),
            "url": f"https://www.reddit.com/r/{sub}/new/.rss",
        })
    for f in cfg.get("rss", []):
        feeds.append({"kind": "rss", **f})
    return feeds


def fetch_feed(feed: dict) -> list:
    if feed["kind"] == "reddit":
        time.sleep(2)  # Reddit rate-limits rapid unauthenticated requests
    for attempt in range(2):
        r = requests.get(feed["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 429 and attempt == 0:
            time.sleep(6)
            continue
        r.raise_for_status()
        return feedparser.parse(r.content).entries
    return []


def normalize(entry, feed: dict, now: float) -> dict:
    title = clean(entry.get("title", ""))
    link = entry.get("link", "") or ""
    source, src_url = feed.get("name", "?"), link

    if feed["kind"] == "gnews":
        src = entry.get("source") or {}
        if src.get("title"):
            source = src["title"]
            suffix = " - " + source
            if title.endswith(suffix):
                title = title[: -len(suffix)]
        src_url = src.get("href") or link
    elif feed["kind"] == "bing":
        target = parse_qs(urlparse(link).query).get("url", [None])[0]
        if target:
            link = src_url = target
        source = clean(entry.get("news_source", "")) or domain_of(link) or source

    t = entry.get("published_parsed") or entry.get("updated_parsed")
    ts = calendar.timegm(t) if t else now
    return {
        "id": hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16],
        "title": title,
        "summary": clean(entry.get("summary", ""))[:600],
        "link": link,
        "source": source,
        "domain": domain_of(src_url),
        "ts": min(ts, now),
        "feed": feed,
    }

# ---------------------------------------------------------------- scoring

def has_kw(text: str, kw: str) -> bool:
    kw = fold(kw)
    if CJK.search(kw) or not kw.isascii():
        return kw in text
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None


# Short units (k, m, bn) only count when glued to the number ("$5m"), so "$41,800 m..." stays $41,800.
MONEY = re.compile(
    r"(?:us\$|ca\$|a\$|c\$|\$|£|€)\s?(\d[\d,]*(?:\.\d+)?)(?:(k|m|mn|bn|mil)|\s*(thousand|million|billion))?(?![a-z0-9])"
    r"|(\d+(?:\.\d+)?)\s*(million|billion)(?![a-z])"
)
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mil": 1e6, "million": 1e6, "billion": 1e9, "bn": 1e9}


def biggest_amount(text: str) -> float:
    best = 0.0
    for m in MONEY.finditer(text):
        if m.group(1):
            num, unit = m.group(1), m.group(2) or m.group(3)
        else:
            num, unit = m.group(4), m.group(5)
        try:
            value = float(num.replace(",", "")) * MULT.get(unit or "", 1)
        except ValueError:
            continue
        best = max(best, value)
    return best


def aggregator_name(item: dict, cfg: dict) -> str | None:
    d, s = item["domain"], fold(item["source"])
    for dom, name in cfg.get("aggregators", {}).items():
        if d == dom or d.endswith("." + dom) or fold(name) in s:
            return name
    return None


def score(item: dict, cfg: dict, now: float):
    sc = cfg["scoring"]
    text = fold(item["title"] + " " + item["summary"])
    pts, why = item["feed"].get("weight", 0), []

    for cat, spec in sc["categories"].items():
        hit = next((k for k in spec["words"] if has_kw(text, k)), None)
        if hit:
            pts += spec["points"]
            why.append(f"{cat} ({hit})")

    amount = biggest_amount(text)
    if amount >= 1_000_000:
        pts += 3
        why.append(f"${amount / 1e6:.1f}M")
    elif amount >= 10_000:
        pts += 2
        why.append(f"${amount / 1e3:.0f}K")

    if any(has_kw(text, k) for k in sc["off_topic"]):
        pts -= 5
        why.append("off-topic")
    if not item["feed"].get("tcg_context") and not any(has_kw(text, k) for k in sc["tcg_terms"]):
        pts -= 3
        why.append("not clearly TCG")

    covered_by = aggregator_name(item, cfg)
    if covered_by:
        pts -= 1
    else:
        pts += 2
        why.append("upstream source")

    if now - item["ts"] < 2 * 3600:
        pts += 1
    return pts, why, covered_by

# ---------------------------------------------------------------- story clustering

def tokens(title: str) -> set[str]:
    t = fold(title)
    if CJK.search(t):
        s = re.sub(r"\s+", "", t)
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2 and w not in STOPWORDS}


def same_story(a, b) -> bool:
    a, b = set(a), set(b)
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 3 and shared / min(len(a), len(b)) >= 0.5

# ---------------------------------------------------------------- notifications

def notify(title: str, body: str, link: str | None = None, priority: int = 3, tags=None) -> bool:
    sent = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        payload = {"topic": topic, "title": title, "message": body, "priority": priority, "tags": tags or []}
        if link:
            payload["click"] = link
        headers = {}
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        try:
            requests.post(server, json=payload, headers=headers, timeout=15).raise_for_status()
            sent = True
        except Exception as e:
            print(f"ntfy failed: {e}", file=sys.stderr)

    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if hook:
        content = f"**{title}**\n{body}" + (f"\n{link}" if link else "")
        try:
            requests.post(hook, json={"content": content[:1990]}, timeout=15).raise_for_status()
            sent = True
        except Exception as e:
            print(f"discord failed: {e}", file=sys.stderr)

    if not topic and not hook:
        print(f"[no notifier configured]\n{title}\n{body}\n{link or ''}\n")
    return sent


def alert_payload(kind: str, item: dict, pts: int, why: list, story: dict, now: float):
    if kind == "new":
        if story.get("covered_by"):
            status = f"Already on {story['covered_by']}. Beat their packaging."
            tags = ["eyes"]
        else:
            status = "EARLY: big accounts don't have this yet."
            tags = ["zap"]
        body = (f"{status}\n{item['source']} · {fmt_age(now - item['ts'])} · score {pts}\n"
                f"Why: {', '.join(why) or '-'}")
        prio = 5 if pts >= 9 else 4 if pts >= 7 else 3
        return item["title"][:200], body, item["link"], prio, tags

    big = (f"{story['covered_by']} has it now." if story.get("covered_by")
           else "No big account has it yet. Post now.")
    body = f"{len(story['sources'])} outlets: {', '.join(story['sources'][:5])}\n{big}"
    return ("Spreading: " + story["title"])[:200], body, item["link"], 4, ["fire"]

# ---------------------------------------------------------------- main loop

def run_once(cfg: dict, state_path: str, dry_run: bool = False) -> list:
    now = time.time()
    state = None if dry_run else load_json(state_path)
    seeding = state is None and not dry_run
    state = state or {"seen": {}, "stories": []}
    seen, stories = state["seen"], state["stories"]

    feeds = build_feeds(cfg)
    items, failed = [], []
    for feed in feeds:
        try:
            for entry in fetch_feed(feed):
                it = normalize(entry, feed, now)
                if it["title"]:
                    items.append(it)
        except Exception as e:
            failed.append(f"{feed['name']}: {type(e).__name__}: {e}"[:180])
    items.sort(key=lambda i: i["ts"])

    max_age = cfg.get("max_age_hours", 12) * 3600
    min_score = cfg.get("min_score", 5)
    alerts, rows = [], []

    for it in items:
        if it["id"] in seen:
            continue
        seen[it["id"]] = now
        if now - it["ts"] > max_age:
            continue
        pts, why, covered = score(it, cfg, now)
        rows.append((pts, it, why, covered))

        toks = tokens(it["title"])
        story = next((s for s in stories if same_story(s["tokens"], toks)), None)
        if story is None:
            story = {"title": it["title"], "tokens": sorted(toks), "first_ts": it["ts"],
                     "sources": [it["source"]], "covered_by": covered,
                     "alerted": False, "spread_alerted": False, "last": now}
            stories.append(story)
        else:
            story["last"] = now
            if it["source"] not in story["sources"]:
                story["sources"].append(it["source"])
            if covered and not story.get("covered_by"):
                story["covered_by"] = covered

        if seeding:
            continue
        if not story["alerted"] and pts >= min_score:
            story["alerted"] = True
            alerts.append(("new", it, pts, why, story))
        elif (story["alerted"] and not story["spread_alerted"]
              and len(story["sources"]) >= cfg.get("spread_threshold", 3)):
            story["spread_alerted"] = True
            alerts.append(("spread", it, pts, why, story))

    if dry_run:
        rows.sort(key=lambda r: -r[0])
        print(f"{'score':>5}  {'age':>9}  {'source':<24} title")
        for pts, it, why, covered in rows[:60]:
            flag = "*" if pts >= min_score else " "
            print(f"{pts:>4}{flag}  {fmt_age(now - it['ts']):>9}  {it['source'][:24]:<24} {it['title'][:90]}")
            print(f"{'':>17}{'covered by ' + covered if covered else 'upstream'} | {', '.join(why)}")
    else:
        if seeding:
            notify("PokeRadar is live",
                   f"Watching {len(feeds) - len(failed)}/{len(feeds)} feeds. "
                   f"You'll only hear from me when a story scores {min_score}+.",
                   None, 3, ["satellite"])
        cap = cfg.get("max_alerts_per_run", 6)
        alerts.sort(key=lambda a: -a[2])
        for a in alerts[:cap]:
            notify(*alert_payload(*a, now))
        if len(alerts) > cap:
            extra = alerts[cap:]
            notify(f"+{len(extra)} more stories",
                   "\n".join(f"• {a[1]['title'][:90]}" for a in extra[:10]), None, 2, ["inbox_tray"])

        # prune: forget items after 4 days, stories after 3 days of silence
        state["seen"] = {k: v for k, v in seen.items() if now - v < 4 * 86400}
        state["stories"] = [s for s in stories if now - s["last"] < 3 * 86400]
        save_json(state_path, state)

    mode = "dry run" if dry_run else ("seeded (first run, no alerts)" if seeding else "scan")
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {mode}: {len(feeds) - len(failed)}/{len(feeds)} feeds ok, "
          f"{len(rows)} fresh new items, {len(alerts)} alerts")
    for f in failed:
        print(f"  feed failed: {f}")
    return alerts


def main():
    ap = argparse.ArgumentParser(description="PokeRadar: Pokémon TCG news radar")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--state", default=os.path.join(HERE, "state", "state.json"))
    ap.add_argument("--dry-run", action="store_true", help="print scores, send nothing, save nothing")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="keep scanning every N seconds (min 60)")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_json(args.config)
    if cfg is None:
        sys.exit(f"config not found: {args.config}")

    if args.test_notify:
        ok = notify("PokeRadar test", "If you can read this, alerts work.", "https://x.com", 3, ["white_check_mark"])
        sys.exit(0 if ok else 1)

    while True:
        try:
            run_once(cfg, args.state, args.dry_run)
        except Exception as e:
            print(f"scan crashed: {type(e).__name__}: {e}", file=sys.stderr)
        if not args.loop:
            break
        time.sleep(max(60, args.loop))


if __name__ == "__main__":
    main()
