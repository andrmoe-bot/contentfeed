#!/usr/bin/env python3
"""Content feed: turns a plain list of links and subscriptions into a browsable feed, served on the LAN.

All personal data lives in the data directory (--data-dir, default ./data), never in the code:
links.txt holds single links and feeds.txt holds subscriptions (RSS/Atom feeds, or pages that have
one, such as YouTube channels). Both have one URL per line, optionally followed by tags; lines
starting with "#" are comments. They are created with instructions on first run.

For each link the server fetches the page once and extracts title, description, image and site
name from OpenGraph / HTML metadata (cached in cache.json). Subscriptions are re-checked on a timer
and their posts are cached in feeds.json.
"""

import argparse
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import feeds
import ranking

ROOT = Path(__file__).resolve().parent  # code and pages; the server never writes here
DEFAULT_DATA_DIR = ROOT / "data"
# Data files; set_data_dir() points these into the data directory at startup.
LINKS_FILE = FEEDS_FILE = CACHE_FILE = ADDED_FILE = FEED_CACHE_FILE = VIEWED_FILE = Path()
TEMPLATES = {
    "links.txt": """\
# One link per line, optionally followed by tags: https://example.com music longread
# Lines starting with # are ignored.
# Newest links go at the bottom; they show up first in the feed.
""",
    "feeds.txt": """\
# Subscriptions: one per line, optionally followed by tags, like links.txt.
# A line can be an RSS/Atom feed, or a page that has one: a YouTube channel (@handle, /channel/…)
# or playlist, a subreddit, a blog, a Mastodon profile, and so on. Lines starting with # are ignored.
#
# https://www.youtube.com/@veritasium science video
# https://www.reddit.com/r/python programming
# https://xkcd.com comics
""",
}
PAGES = {  # request path -> (file, content type)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/subscriptions": ("subscriptions.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
}
MAX_BYTES = 1_000_000
MAX_ITEMS_SHOWN = 300

lock = threading.Lock()
cache: dict[str, dict] = {}
added: dict[str, float] = {}  # url -> unix time the server first saw the link
viewed: dict[str, float] = {}  # url -> unix time you last opened it
subscriptions: dict[str, dict] = {}  # feeds.txt url -> resolved feed, its posts and fetch status
pending: set[str] = set()
feed_interval = 30 * 60  # seconds between checks of each subscription; set by --feed-interval
hide_viewed = 30 * 86400  # seconds an opened item stays hidden (math.inf = until unmarked); --hide-viewed
executor = ThreadPoolExecutor(max_workers=8)


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.icon: str | None = None
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "link" and "icon" in a.get("rel", "").lower() and not self.icon:
            self.icon = a.get("href")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def set_data_dir(path: Path):
    """Use path for all data files, creating it and the starter lists if they don't exist yet."""
    global LINKS_FILE, FEEDS_FILE, CACHE_FILE, ADDED_FILE, FEED_CACHE_FILE, VIEWED_FILE
    path.mkdir(parents=True, exist_ok=True)
    LINKS_FILE, FEEDS_FILE = path / "links.txt", path / "feeds.txt"
    CACHE_FILE, ADDED_FILE, FEED_CACHE_FILE = path / "cache.json", path / "added.json", path / "feeds.json"
    VIEWED_FILE = path / "viewed.json"
    for name, text in TEMPLATES.items():
        if not (path / name).exists():
            (path / name).write_text(text, encoding="utf-8")


def read_entries(path: Path | None = None) -> dict[str, list[str]]:
    """url -> tags, in file order (links.txt by default). A line is a URL followed by optional tags."""
    path = path or LINKS_FILE
    if not path.exists():
        return {}
    entries: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        url, *tags = line.split() or [""]
        if url.startswith("#") or urlparse(url).scheme not in ("http", "https") or url in entries:
            continue
        entries[url] = clean_tags(tags)
    return entries


def clean_tags(words: list[str]) -> list[str]:
    """Tags are lowercase words without a leading '#'; commas also separate them."""
    tags = []
    for word in " ".join(words).replace(",", " ").split():
        tag = word.lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def read_links() -> list[str]:
    return list(read_entries())


def fetch_metadata(url: str) -> dict:
    item = {"url": url, "domain": urlparse(url).netloc.removeprefix("www."), "fetched": time.time()}
    yt = feeds.youtube_id(url)
    if yt:
        item["youtube"] = yt
        item["image"] = f"https://i.ytimg.com/vi/{yt}/hqdefault.jpg"
    try:
        with feeds.open_url(url, "text/html,*/*") as resp:
            ctype = resp.headers.get_content_type()
            final_url = resp.geturl()
            if ctype.startswith("image/"):
                item["image"] = final_url
            elif "html" in ctype:
                charset = resp.headers.get_content_charset() or "utf-8"
                parser = MetaParser()
                parser.feed(resp.read(MAX_BYTES).decode(charset, errors="replace"))
                m = parser.meta
                item["title"] = m.get("og:title") or m.get("twitter:title") or parser.title.strip()
                item["description"] = (
                    m.get("og:description") or m.get("twitter:description") or m.get("description") or ""
                )
                img = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image")
                if img and "image" not in item:
                    item["image"] = urljoin(final_url, img)
                item["site"] = m.get("og:site_name") or ""
                item["icon"] = urljoin(final_url, parser.icon or "/favicon.ico")
    except Exception as e:  # network errors, bad encodings, etc. — still show the link
        item["error"] = str(e)[:200]
    item["title"] = (item.get("title") or url)[:300]
    item["description"] = (item.get("description") or "")[:500]
    return item


def save_json(path: Path, obj: dict):
    with lock:
        data = json.dumps(obj, indent=1)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def refresh(url: str):
    try:
        item = fetch_metadata(url)
        with lock:
            cache[url] = item
        save_json(CACHE_FILE, cache)
    finally:
        with lock:
            pending.discard(url)


def schedule(url: str, force: bool = False):
    with lock:
        if url in pending or (url in cache and not force):
            return
        pending.add(url)
    executor.submit(refresh, url)


def refresh_subscription(url: str, force: bool = False):
    """Check one subscription and merge its posts, keeping when each post was first seen."""
    try:
        with lock:
            old = dict(subscriptions.get(url, {}))
        try:
            feed_url = old.get("feed_url") or feeds.discover(url)
            fresh = feeds.fetch(feed_url, *(() if force else (old.get("etag"), old.get("modified"))))
        except Exception as e:  # keep the old posts; show the error in the subscriptions list
            with lock:
                subscriptions[url] = {**old, "error": str(e)[:300], "fetched": time.time()}
            save_json(FEED_CACHE_FILE, subscriptions)
            return
        now = time.time()
        if fresh is None:  # not modified since last check
            entry = {**old, "fetched": now}
        else:
            first_seen = {it["url"]: it.get("first_seen", now) for it in old.get("items", [])}
            for it in fresh["items"]:
                it["first_seen"] = first_seen.get(it["url"], now)
            entry = fresh
        entry.pop("error", None)
        with lock:
            subscriptions[url] = entry
        save_json(FEED_CACHE_FILE, subscriptions)
    finally:
        with lock:
            pending.discard("feed:" + url)


def schedule_subscription(url: str, force: bool = False):
    key = "feed:" + url
    with lock:
        last = subscriptions.get(url, {}).get("fetched", 0)
        if key in pending or (not force and time.time() - last < feed_interval):
            return
        pending.add(key)
    executor.submit(refresh_subscription, url, force)


def subscribe(line: str) -> str | None:
    """Add a feeds.txt line (a URL optionally followed by tags). Returns an error message, or None."""
    url, *tags = line.split() or [""]
    if urlparse(url).scheme not in ("http", "https"):
        return "That isn't a web address."
    if url in read_entries(FEEDS_FILE):
        return "Already subscribed."
    with lock:
        pending.add("feed:" + url)
    refresh_subscription(url, force=True)
    with lock:
        error = subscriptions.get(url, {}).get("error")
        if error:
            subscriptions.pop(url, None)
    if error:
        save_json(FEED_CACHE_FILE, subscriptions)
        return error
    append_line(FEEDS_FILE, url, clean_tags(tags))
    return None


def subscription_status() -> list[dict]:
    status = []
    with lock:
        for url, tags in read_entries(FEEDS_FILE).items():
            s = subscriptions.get(url, {})
            dates = [it["published"] or it["first_seen"] for it in s.get("items", [])]
            status.append({
                "url": url,
                "tags": tags,
                "title": s.get("title") or url,
                "site": s.get("site"),
                "icon": s.get("icon"),
                "feed_url": s.get("feed_url"),
                "posts": len(s.get("items", [])),
                "latest": max(dates, default=None),
                "fetched": s.get("fetched"),
                "error": s.get("error"),
                "checking": "feed:" + url in pending,
            })
    return status


def replace_line(path: Path, url: str, new_line: str | None) -> bool:
    """Replace (or with None, delete) the line for url, leaving comments and other lines as they are."""
    with lock:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for i, line in enumerate(lines):
            if (line.split() or [""])[0] == url:
                if new_line is None:
                    del lines[i]
                else:
                    lines[i] = new_line
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return True
    return False


def set_subscription_tags(url: str, tags: list[str]) -> bool:
    return replace_line(FEEDS_FILE, url, " ".join([url, *clean_tags(tags)]))


def unsubscribe(url: str) -> bool:
    if not replace_line(FEEDS_FILE, url, None):
        return False
    with lock:
        subscriptions.pop(url, None)
    save_json(FEED_CACHE_FILE, subscriptions)
    return True


def check_subscription(url: str) -> bool:
    """Check one subscription now and wait for the result."""
    if url not in read_entries(FEEDS_FILE):
        return False
    with lock:
        pending.add("feed:" + url)
    refresh_subscription(url, force=True)
    return True


def poll_subscriptions():
    while True:
        for url in read_entries(FEEDS_FILE):
            schedule_subscription(url)
        time.sleep(60)


DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text: str) -> float:
    """'30d', '12h', '2w', '90m', 'never' (hide until unmarked) or '0' (don't hide) -> seconds."""
    text = text.strip().lower()
    if text == "never":
        return math.inf
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhdw]?)", text)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r}; use e.g. 30d, 12h, 2w, 90m, never or 0")
    return float(m.group(1)) * DURATION_UNITS[m.group(2) or "d"]


def describe_duration(seconds: float) -> str:
    """How long opened items stay hidden, as a phrase: 'for 30 days', 'until unmarked'."""
    if seconds == math.inf:
        return "until unmarked"
    for unit, size in (("week", 7 * 86400), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = int(seconds // size)
            return f"for {n} {unit}{'s' if n != 1 else ''}"
    return f"for {seconds / 86400:g} days"


def view_state(url: str, now: float) -> dict:
    """Last-viewed time, and whether that hides the item right now. Call with lock held."""
    last = viewed.get(url)
    if last is None or hide_viewed <= 0:
        return {"last_viewed": last, "hidden": False, "returns": None}
    returns = last + hide_viewed
    if returns <= now:
        return {"last_viewed": last, "hidden": False, "returns": None}
    return {"last_viewed": last, "hidden": True, "returns": None if returns == math.inf else returns}


def known_urls() -> set[str]:
    urls = set(read_entries())
    subs = read_entries(FEEDS_FILE)
    with lock:
        for sub_url in subs:
            urls.update(post["url"] for post in subscriptions.get(sub_url, {}).get("items", []))
    return urls


def set_viewed(url: str, is_viewed: bool) -> bool:
    """Record that you opened url now, or forget that you did. Only for items in the feed."""
    if is_viewed and url not in known_urls():
        return False
    with lock:
        if is_viewed:
            viewed[url] = time.time()
        elif viewed.pop(url, None) is None:
            return False
    save_json(VIEWED_FILE, viewed)
    return True


def feed(ranker: str) -> dict:
    entries = read_entries()
    subs = read_entries(FEEDS_FILE)
    for url in entries:
        schedule(url)
    for url in subs:
        schedule_subscription(url)
    now = time.time()
    with lock:
        new = [url for url in entries if url not in added]
        added.update({url: now for url in new})
        items = [
            {**cache.get(url, {"url": url, "loading": True}), "kind": "link", "tags": tags,
             "added": added[url], "date": added[url], "position": i}
            for i, (url, tags) in enumerate(entries.items())
        ]
        seen = set(entries)
        for sub_url, tags in subs.items():
            s = subscriptions.get(sub_url, {})
            for post in s.get("items", []):
                if post["url"] in seen:
                    continue
                seen.add(post["url"])
                items.append({
                    **post, "kind": "subscription", "tags": tags, "feed": s["title"], "icon": s.get("icon"),
                    "domain": urlparse(post["url"]).netloc.removeprefix("www."),
                    "added": post["first_seen"], "date": post["published"] or post["first_seen"], "position": -1,
                })
        for item in items:
            item.update(view_state(item["url"], now))
        n_pending = len(pending)
    if new:
        save_json(ADDED_FILE, added)
    ranked = ranking.rank(ranker, items, now)
    # Hidden items stay in their ranked place, so an item you just opened keeps its spot on the page.
    shown, n_visible, n_hidden = [], 0, 0
    for item in ranked:
        if item["hidden"]:
            n_hidden += 1
            if n_hidden <= MAX_ITEMS_SHOWN:
                shown.append(item)
        else:
            n_visible += 1
            if n_visible <= MAX_ITEMS_SHOWN:
                shown.append(item)
    return {
        "items": shown,
        "total": n_visible,
        "hidden": n_hidden,
        "hide_viewed": describe_duration(hide_viewed) if hide_viewed > 0 else None,
        "pending": n_pending,
        "ranker": ranker,
    }


def append_line(path: Path, url: str, tags: list[str]):
    with lock:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + " ".join([url, *tags]) + "\n", encoding="utf-8")


def add_link(line: str) -> bool:
    """Append a links.txt line: a URL optionally followed by tags."""
    url, *tags = line.split() or [""]
    if urlparse(url).scheme not in ("http", "https") or url in read_links():
        return False
    append_line(LINKS_FILE, url, clean_tags(tags))
    schedule(url)
    return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in PAGES:
            name, ctype = PAGES[path]
            self._send(200, (ROOT / name).read_bytes(), ctype)
        elif path == "/api/feed":
            name = parse_qs(parsed.query).get("ranker", [self.server.ranker])[0]
            if name not in ranking.RANKERS:
                return self._json({"error": f"unknown ranker; available: {sorted(ranking.RANKERS)}"}, 400)
            self._json(feed(name))
        elif path == "/api/subscriptions":
            self._json({"subscriptions": subscription_status(), "interval_minutes": feed_interval / 60})
        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid json"}, 400)
        if path == "/api/links":
            ok = add_link(str(body.get("url", "")))
            self._json({"ok": ok}, 200 if ok else 400)
        elif path == "/api/subscriptions":
            error = subscribe(str(body.get("url", "")))
            self._json({"ok": not error, "error": error}, 400 if error else 200)
        elif path in ("/api/subscriptions/tags", "/api/subscriptions/remove", "/api/subscriptions/check"):
            url = str(body.get("url", ""))
            if path.endswith("/tags"):
                tags = body.get("tags", [])
                ok = set_subscription_tags(url, [str(t) for t in tags] if isinstance(tags, list) else [str(tags)])
            elif path.endswith("/remove"):
                ok = unsubscribe(url)
            else:
                ok = check_subscription(url)
            self._json({"ok": ok, "subscriptions": subscription_status()}, 200 if ok else 404)
        elif path in ("/api/viewed", "/api/viewed/remove"):
            ok = set_viewed(str(body.get("url", "")), is_viewed=path == "/api/viewed")
            self._json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/refresh":
            for u in read_links():
                schedule(u, force=True)
            for u in read_entries(FEEDS_FILE):
                schedule_subscription(u, force=True)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


def lan_addresses() -> list[str]:
    import socket

    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(addrs) or ["<this-machine-ip>"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=80, help="default 80; ports below 1024 need extra permission")
    ap.add_argument("--ranker", default=ranking.DEFAULT, choices=sorted(ranking.RANKERS))
    ap.add_argument("--feed-interval", type=float, default=30, help="minutes between subscription checks")
    ap.add_argument("--hide-viewed", type=parse_duration, default="30d",
                    help="how long an item stays hidden after you open it: e.g. 30d, 12h, 2w, never, or 0 to "
                         "not hide (default: 30d)")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help="where links.txt, feeds.txt and the caches are kept (default: ./data)")
    args = ap.parse_args()

    global feed_interval, hide_viewed
    feed_interval = max(args.feed_interval, 1) * 60
    hide_viewed = args.hide_viewed
    set_data_dir(args.data_dir.resolve())
    cache.update(load_json(CACHE_FILE))
    added.update(load_json(ADDED_FILE))
    subscriptions.update(load_json(FEED_CACHE_FILE))
    viewed.update(load_json(VIEWED_FILE))
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except PermissionError:
        raise SystemExit(
            f"Not allowed to use port {args.port}: ports below 1024 need root or CAP_NET_BIND_SERVICE "
            f"(the systemd service grants this). For development, use e.g. --port 8090."
        )
    except OSError as e:
        raise SystemExit(f"Can't use port {args.port}: {e.strerror}. Choose another with --port.")
    server.ranker = args.ranker
    feed(args.ranker)  # warm the caches in the background
    threading.Thread(target=poll_subscriptions, daemon=True).start()

    suffix = "" if args.port == 80 else f":{args.port}"
    print(f"Content feed using data in {LINKS_FILE.parent}, serving on:")
    print(f"  http://localhost{suffix}")
    for ip in lan_addresses():
        print(f"  http://{ip}{suffix}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
