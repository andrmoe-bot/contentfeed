"""Subscriptions: finding, fetching and parsing public feeds (RSS 2.0, RSS 1.0 and Atom).

A subscription can be given as the feed itself or as a page that has one: a YouTube channel,
handle or playlist, a subreddit, a blog, a Mastodon profile, and so on.
"""

import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

USER_AGENT = "Mozilla/5.0 (compatible; ContentFeed/1.0)"
FETCH_TIMEOUT = 10
MAX_FEED_BYTES = 5_000_000
MAX_ITEMS_PER_FEED = 50
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+xml", "application/rdf+xml")


class FeedError(Exception):
    pass


def open_url(url: str, accept: str, headers: dict | None = None):
    """urlopen with our User-Agent. YouTube gets a cookie that skips its EU consent page."""
    h = {"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})}
    if urlparse(url).netloc.lower().endswith("youtube.com"):
        h["Cookie"] = "SOCS=CAI"
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=FETCH_TIMEOUT)


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


def known_feed_url(url: str) -> str | None:
    """Feed URLs that can be worked out from the address alone, without fetching anything."""
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("old.")
    q = parse_qs(p.query)
    if host == "youtube.com":
        m = re.match(r"^/channel/(UC[\w-]+)", p.path)
        if m:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
        if p.path == "/playlist" and "list" in q:
            return f"https://www.youtube.com/feeds/videos.xml?playlist_id={q['list'][0]}"
    if host == "reddit.com" and re.match(r"^/(r|user|u)/[^/]+/?$", p.path):
        return f"https://www.reddit.com{p.path.rstrip('/')}/.rss"
    return None


class LinkFinder(HTMLParser):
    """Collects <link rel="alternate" type="...rss/atom..."> feed links from a page."""

    def __init__(self):
        super().__init__()
        self.feeds: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link" and "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() in FEED_TYPES:
            self.feeds.append(a.get("href", ""))


def fetch(url: str, etag: str | None = None, modified: str | None = None) -> dict | None:
    """Fetch and parse a feed. Returns None if unchanged since etag/modified (HTTP 304)."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    try:
        with open_url(url, "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5", headers) as r:
            body = r.read(MAX_FEED_BYTES)
            final_url = r.geturl()
            resp_etag, resp_modified = r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise FeedError(f"HTTP {e.code} from {url}") from e
    except OSError as e:
        raise FeedError(f"couldn't fetch {url}: {e}") from e
    feed = parse(body, final_url)
    feed.update(feed_url=final_url, etag=resp_etag, modified=resp_modified)
    return feed


def discover(url: str) -> str:
    """Return the feed URL for a subscription: the URL itself if it is a feed, or the feed a page links to."""
    known = known_feed_url(url)
    if known:
        return known
    try:
        with open_url(url, "text/html, application/rss+xml, application/atom+xml, */*;q=0.5") as r:
            body = r.read(MAX_FEED_BYTES)
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        raise FeedError(f"HTTP {e.code} from {url}") from e
    except OSError as e:
        raise FeedError(f"couldn't fetch {url}: {e}") from e
    try:
        parse(body, final_url)
        return final_url
    except FeedError:
        pass  # not a feed itself; look for one on the page
    text = body.decode("utf-8", errors="replace")
    finder = LinkFinder()
    finder.feed(text)
    if finder.feeds:
        return urljoin(final_url, finder.feeds[0])
    # YouTube pages without the <link> tag still mention the channel id in their inline data
    m = re.search(r"feeds/videos\.xml\?channel_id=(UC[\w-]+)", text) or re.search(
        r'"(?:externalId|channelId)":"(UC[\w-]+)"', text
    )
    if m:
        return known_feed_url(f"https://www.youtube.com/channel/{m.group(1)}")
    raise FeedError(f"no RSS or Atom feed found at {url}")


def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child(el, name: str):
    return next((c for c in el if local(c.tag) == name), None)


def text_of(el, *names: str) -> str:
    for name in names:
        c = child(el, name)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


class TextAndImage(HTMLParser):
    """Plain text of an HTML fragment, plus its first image."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.image: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "img" and not self.image:
            self.image = dict(attrs).get("src")
        if tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        self.parts.append(data)


BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td"}


def html_to_text(fragment: str) -> tuple[str, str | None]:
    p = TextAndImage()
    p.feed(fragment)
    return " ".join("".join(p.parts).split()), p.image


def parse_date(value: str) -> float | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.strip())  # Atom: ISO 8601
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def item_link(entry) -> str:
    for c in entry:
        if local(c.tag) == "link":
            if c.get("href") and c.get("rel", "alternate") == "alternate":
                return c.get("href")  # Atom
            if (c.text or "").strip():
                return c.text.strip()  # RSS
    guid = child(entry, "guid")
    if guid is not None and guid.get("isPermaLink", "true") == "true" and (guid.text or "").startswith("http"):
        return guid.text.strip()
    return ""


def item_image(entry) -> str | None:
    for el in entry.iter():
        name = local(el.tag)
        if name == "thumbnail" and el.get("url"):  # media:thumbnail
            return el.get("url")
        if name == "content" and el.get("url") and (el.get("medium") == "image" or el.get("type", "").startswith("image/")):
            return el.get("url")  # media:content
        if name == "enclosure" and el.get("type", "").startswith("image/"):
            return el.get("url")
        if name == "image" and el.get("href"):  # itunes:image
            return el.get("href")
    return None


def parse(body: bytes, base_url: str) -> dict:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom into {title, site, items}."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedError(f"not a valid feed ({e})") from e
    if local(root.tag) not in ("rss", "feed", "RDF"):
        raise FeedError(f"not an RSS or Atom feed (root element <{local(root.tag)}>)")
    channel = child(root, "channel") if local(root.tag) != "feed" else root
    channel = channel if channel is not None else root
    site = item_link(channel) or base_url

    items = []
    for entry in root.iter():
        if local(entry.tag) not in ("item", "entry"):
            continue
        link = urljoin(base_url, item_link(entry))
        if not link.startswith("http"):
            continue
        raw = ""
        media_group = child(entry, "group")  # YouTube puts the description in media:group
        if media_group is not None:
            raw = text_of(media_group, "description")
        raw = raw or text_of(entry, "summary", "description", "encoded", "content")
        description, inline_image = html_to_text(raw)
        title = html_to_text(text_of(entry, "title"))[0]
        if not title:  # e.g. Mastodon posts have no title; use the start of the text
            title, description = description[:120], (description if len(description) > 120 else "")
        image = item_image(entry) or inline_image
        video = text_of(entry, "videoId") or youtube_id(link)
        items.append({
            "url": link,
            "title": (title or link)[:300],
            "description": description[:500],
            "image": urljoin(link, image) if image else None,
            "youtube": video or None,
            "published": parse_date(text_of(entry, "published", "pubDate", "date", "updated", "issued")),
        })
    items.sort(key=lambda it: it["published"] or 0, reverse=True)
    return {
        "title": html_to_text(text_of(channel, "title"))[0] or urlparse(base_url).netloc,
        "site": site,
        "icon": urljoin(site, "/favicon.ico"),
        "items": items[:MAX_ITEMS_PER_FEED],
        "fetched": time.time(),
    }
