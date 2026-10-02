#!/usr/bin/env python3
"""Content feed: turns a plain list of links and subscriptions into a browsable feed, served on the LAN.

All personal data lives in the data directory (--data-dir, default ./data), never in the code:
links.txt holds single links and feeds.txt holds subscriptions (RSS/Atom feeds, or pages that have
one, such as YouTube channels). Both have one URL per line, optionally followed by tags; lines
starting with "#" are comments. They are created with instructions on first run.

For each link the server fetches the page once and extracts title, description, image and site
name from OpenGraph / HTML metadata (cached in cache.json). Subscriptions are re-checked on a timer
and their posts are kept in feeds.json, including posts that have since left the feed. For YouTube
channels and playlists, older videos than the feed lists are loaded once (see feeds.older_videos).
"""

import argparse
import json
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
LINKS_FILE = FEEDS_FILE = CACHE_FILE = ADDED_FILE = FEED_CACHE_FILE = VIEWED_FILE = COLORS_FILE = SETTINGS_FILE = Path()
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
    "/settings": ("settings.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
}
MAX_BYTES = 1_000_000
MAX_ITEMS_SHOWN = 300  # per page of the feed; "Show more" asks for the next page
MAX_POSTS_KEPT = 5000  # per subscription, newest first
OLDER_RETRY = 6 * 3600  # seconds before trying again to load a channel's older videos after a failure

lock = threading.Lock()
cache: dict[str, dict] = {}
added: dict[str, float] = {}  # url -> unix time the server first saw the link
viewed: dict[str, float] = {}  # url -> unix time you last opened it
colors: dict[str, str] = {}  # url -> color you gave it; items without one are white
subscriptions: dict[str, dict] = {}  # feeds.txt url -> resolved feed, its posts and fetch status
pending: set[str] = set()
feed_interval = 30 * 60  # seconds between checks of each subscription; set by --feed-interval
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
    global LINKS_FILE, FEEDS_FILE, CACHE_FILE, ADDED_FILE, FEED_CACHE_FILE, VIEWED_FILE, COLORS_FILE, SETTINGS_FILE
    path.mkdir(parents=True, exist_ok=True)
    LINKS_FILE, FEEDS_FILE = path / "links.txt", path / "feeds.txt"
    CACHE_FILE, ADDED_FILE, FEED_CACHE_FILE = path / "cache.json", path / "added.json", path / "feeds.json"
    VIEWED_FILE, COLORS_FILE = path / "viewed.json", path / "colors.json"
    SETTINGS_FILE = path / "settings.json"
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


def post_key(post: dict) -> str:
    return post.get("youtube") or post["url"]  # a YouTube short has two urls: /shorts/ID and /watch?v=ID


def merge_posts(old: list[dict], new: list[dict], now: float) -> list[dict]:
    """Old and new posts together, newest first. New posts replace old copies but keep first_seen."""
    first_seen = {post_key(p): p.get("first_seen", now) for p in old}
    merged = {post_key(p): p for p in old}
    for p in new:
        merged[post_key(p)] = {**p, "first_seen": first_seen.get(post_key(p), now)}
    posts = sorted(merged.values(), key=lambda p: p["published"] or p["first_seen"], reverse=True)
    return posts[:MAX_POSTS_KEPT]


def refresh_subscription(url: str, force: bool = False):
    """Check one subscription and merge its posts, keeping posts that have left the feed."""
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
            entry = {**old, **fresh, "items": merge_posts(old.get("items", []), fresh["items"], now)}
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


def load_older(url: str):
    """Add the older videos of a YouTube subscription, beyond the ones its feed lists."""
    try:
        with lock:
            feed_url = subscriptions.get(url, {}).get("feed_url")
        try:
            older = feeds.older_videos(feed_url)
        except feeds.FeedError as e:
            with lock:
                if url in subscriptions:
                    subscriptions[url]["older_error"] = {"error": str(e)[:300], "time": time.time()}
            save_json(FEED_CACHE_FILE, subscriptions)
            return
        now = time.time()
        with lock:
            s = subscriptions.get(url)
            if s is None or s.get("feed_url") != feed_url:  # unsubscribed meanwhile
                return
            known = {post_key(p) for p in s.get("items", [])}
            # Posts already known came from the feed, with exact dates, so they win over these.
            s["items"] = merge_posts(s.get("items", []), [p for p in older if post_key(p) not in known], now)
            s["older_loaded"] = now
            s.pop("older_error", None)
        save_json(FEED_CACHE_FILE, subscriptions)
    finally:
        with lock:
            pending.discard("older:" + url)


def schedule_older(url: str):
    """Load a YouTube subscription's older videos once, after its feed has been read."""
    key = "older:" + url
    with lock:
        s = subscriptions.get(url, {})
        failed = s.get("older_error", {}).get("time", 0)
        if (key in pending or s.get("older_loaded") or not feeds.youtube_playlist_id(s.get("feed_url") or "")
                or time.time() - failed < OLDER_RETRY):
            return
        pending.add(key)
    executor.submit(load_older, url)


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
    schedule_older(url)
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
                "older": "loading" if "older:" + url in pending else "loaded" if s.get("older_loaded") else None,
                "older_error": s.get("older_error", {}).get("error"),
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
            schedule_older(url)
        time.sleep(60)


# Colors you can give an item; ranking.py decides what they do. They have no proper names yet.
COLORS = ("green", "white", "red")


def view_state(url: str) -> dict:
    """The item's color and when you last opened it. Call with lock held."""
    return {"color": colors.get(url, "white"), "last_viewed": viewed.get(url)}


def known_urls() -> set[str]:
    urls = set(read_entries())
    subs = read_entries(FEEDS_FILE)
    with lock:
        for sub_url in subs:
            urls.update(post["url"] for post in subscriptions.get(sub_url, {}).get("items", []))
    return urls


def set_viewed(url: str) -> bool:
    """Record that you opened url now. Only for items in the feed."""
    if url not in known_urls():
        return False
    with lock:
        viewed[url] = time.time()
    save_json(VIEWED_FILE, viewed)
    return True


def set_color(url: str, color: str) -> bool:
    if color not in COLORS or url not in known_urls():
        return False
    with lock:
        if color == "white":
            colors.pop(url, None)
        else:
            colors[url] = color
    save_json(COLORS_FILE, colors)
    return True


def settings() -> dict:
    return {"weights": ranking.WEIGHTS, "defaults": ranking.DEFAULT_WEIGHTS}


def save_settings(weights) -> str | None:
    """Set the score ranker's weights from the Settings page. Returns an error message, or None."""
    try:
        checked = ranking.check_weights(weights if isinstance(weights, dict) else {})
    except ValueError as e:
        return str(e)
    with lock:
        ranking.WEIGHTS.update(checked)
    save_json(SETTINGS_FILE, {"weights": checked})
    return None


def load_settings():
    weights = load_json(SETTINGS_FILE).get("weights")
    if not isinstance(weights, dict):
        return
    # Settings saved by another version may lack newer weights, which keep their defaults, or have
    # ones that no longer exist, which are dropped.
    weights = {k: v for k, v in weights.items() if k in ranking.DEFAULT_WEIGHTS}
    try:
        ranking.WEIGHTS.update(ranking.check_weights({**ranking.DEFAULT_WEIGHTS, **weights}))
    except ValueError as e:
        print(f"Ignoring {SETTINGS_FILE}: {e}")


def next_posts(subs) -> dict[str, str]:
    """For each subscription, the post published right after the one you opened most recently,
    as {its url: the opened post's title}. Call with lock held."""
    out = {}
    for sub_url in subs:
        posts = subscriptions.get(sub_url, {}).get("items", [])  # newest first
        opened = [(viewed[p["url"]], i) for i, p in enumerate(posts) if p["url"] in viewed]
        if opened:
            i = max(opened)[1]
            if i > 0:
                out.setdefault(posts[i - 1]["url"], posts[i]["title"] or posts[i]["url"])
    return out


def last_opened(subs) -> dict[str, float]:
    """For each subscription you've opened a post of, when you last opened one. Call with lock held."""
    out = {}
    for sub_url in subs:
        times = [viewed[p["url"]] for p in subscriptions.get(sub_url, {}).get("items", []) if p["url"] in viewed]
        if times:
            out[sub_url] = max(times)
    return out


def feed(ranker: str, limit: int = MAX_ITEMS_SHOWN) -> dict:
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
        next_after = next_posts(subs)
        sub_opened = last_opened(subs)
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
                    "sub_last_viewed": sub_opened.get(sub_url),
                })
        for item in items:
            item.update(view_state(item["url"]), next_after=next_after.get(item["url"]))
        n_pending = len(pending)
    if new:
        save_json(ADDED_FILE, added)
    ranked = ranking.rank(ranker, items, now)
    return {
        "items": ranked[:limit],
        "total": len(ranked),
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
            query = parse_qs(parsed.query)
            name = query.get("ranker", [self.server.ranker])[0]
            if name not in ranking.RANKERS:
                return self._json({"error": f"unknown ranker; available: {sorted(ranking.RANKERS)}"}, 400)
            limit = query.get("limit", [""])[0]
            self._json(feed(name, max(int(limit), 1) if limit.isdigit() else MAX_ITEMS_SHOWN))
        elif path == "/api/subscriptions":
            self._json({"subscriptions": subscription_status(), "interval_minutes": feed_interval / 60})
        elif path == "/api/settings":
            self._json(settings())
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
        elif path == "/api/viewed":
            ok = set_viewed(str(body.get("url", "")))
            self._json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/color":
            ok = set_color(str(body.get("url", "")), str(body.get("color", "")))
            self._json({"ok": ok}, 200 if ok else 404)
        elif path == "/api/settings":
            error = save_settings(body.get("weights"))
            self._json({"ok": not error, "error": error, **settings()}, 400 if error else 200)
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
    # Items used to be hidden after you opened them; now ranking.py scores them down instead.
    for old in ("--hide-viewed", "--hide-green"):
        ap.add_argument(old, help=argparse.SUPPRESS)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                    help="where links.txt, feeds.txt and the caches are kept (default: ./data)")
    args = ap.parse_args()

    if args.hide_viewed or args.hide_green:
        print("Note: --hide-viewed and --hide-green no longer do anything; nothing is hidden now. "
              "Viewed and colored items are scored instead (see ranking.py).")
    global feed_interval
    feed_interval = max(args.feed_interval, 1) * 60
    set_data_dir(args.data_dir.resolve())
    cache.update(load_json(CACHE_FILE))
    added.update(load_json(ADDED_FILE))
    subscriptions.update(load_json(FEED_CACHE_FILE))
    viewed.update(load_json(VIEWED_FILE))
    colors.update(load_json(COLORS_FILE))
    load_settings()
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
