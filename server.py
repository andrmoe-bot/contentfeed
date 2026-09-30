#!/usr/bin/env python3
"""Content feed: turns a plain list of links into a browsable feed, served on the LAN.

Links live in links.txt (one URL per line, lines starting with "#" are comments). For each link the
server fetches the page once and extracts title, description, image and site name
from OpenGraph / HTML metadata. Results are cached in cache.json.
"""

import argparse
import html
import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

ROOT = Path(__file__).resolve().parent
LINKS_FILE = ROOT / "links.txt"
CACHE_FILE = ROOT / "cache.json"
INDEX_FILE = ROOT / "index.html"
USER_AGENT = "Mozilla/5.0 (compatible; ContentFeed/1.0)"
FETCH_TIMEOUT = 10
MAX_BYTES = 1_000_000

lock = threading.Lock()
cache: dict[str, dict] = {}
pending: set[str] = set()
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


def read_links() -> list[str]:
    if not LINKS_FILE.exists():
        return []
    links = []
    for line in LINKS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and urlparse(line).scheme in ("http", "https") and line not in links:
            links.append(line)
    return links


def youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        return p.path.lstrip("/").split("/")[0] or None
    if host == "youtube.com":
        if p.path == "/watch":
            return parse_qs(p.query).get("v", [None])[0]
        m = re.match(r"^/(shorts|embed|live)/([\w-]+)", p.path)
        if m:
            return m.group(2)
    return None


def fetch_metadata(url: str) -> dict:
    item = {"url": url, "domain": urlparse(url).netloc.removeprefix("www."), "fetched": time.time()}
    yt = youtube_id(url)
    if yt:
        item["youtube"] = yt
        item["image"] = f"https://i.ytimg.com/vi/{yt}/hqdefault.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
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


def save_cache():
    with lock:
        data = json.dumps(cache, indent=1)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(CACHE_FILE)


def refresh(url: str):
    try:
        item = fetch_metadata(url)
        with lock:
            cache[url] = item
        save_cache()
    finally:
        with lock:
            pending.discard(url)


def schedule(url: str, force: bool = False):
    with lock:
        if url in pending or (url in cache and not force):
            return
        pending.add(url)
    executor.submit(refresh, url)


def feed() -> dict:
    links = read_links()
    for url in links:
        schedule(url)
    with lock:
        items = [cache.get(url, {"url": url, "loading": True}) for url in links]
        # newest-first: last line of links.txt is the newest entry
        return {"items": list(reversed(items)), "pending": len(pending)}


def add_link(url: str) -> bool:
    url = url.strip()
    if urlparse(url).scheme not in ("http", "https") or url in read_links():
        return False
    with lock:
        text = LINKS_FILE.read_text(encoding="utf-8") if LINKS_FILE.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        LINKS_FILE.write_text(text + url + "\n", encoding="utf-8")
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
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/feed":
            self._json(feed())
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
        elif path == "/api/refresh":
            url = body.get("url")
            for u in [url] if url else read_links():
                schedule(u, force=True)
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
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    if CACHE_FILE.exists():
        try:
            cache.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    feed()  # warm the cache in the background

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Content feed serving {LINKS_FILE.name} on:")
    print(f"  http://localhost:{args.port}")
    for ip in lan_addresses():
        print(f"  http://{ip}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
