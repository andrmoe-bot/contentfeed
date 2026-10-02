"""Subscriptions to a Jellyfin media server: its movies and TV episodes, as feed posts.

A subscription is the server's address, for everything the user can see, or a page on it (a library,
series, season or collection), such as https://jellyfin.example/web/#/details?id=…, for what's in it.
The login for each server is kept in jellyfin.json in the data directory, never in feeds.txt:

    {"https://jellyfin.example": {"username": "me", "password": "…"}}
"""

import calendar
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from feeds import FETCH_TIMEOUT, FeedError

MAX_ITEMS = 5000  # most recently added first
MAX_BYTES = 50_000_000
USER_AGENT = "ContentFeed/1.0"  # an API client's; some proxies, such as the demo server's, refuse browser-like ones
CLIENT = 'MediaBrowser Client="ContentFeed", Device="ContentFeed", DeviceId="contentfeed", Version="1.0"'

tokens: dict[str, tuple[str, str]] = {}  # server -> (access token, user id), kept in memory only


def server_of(url: str) -> str:
    """The server's base address: everything before /web, without a trailing slash."""
    p = urlparse(url)
    path = re.split(r"/web(?:/|$)", p.path, maxsplit=1)[0].rstrip("/")
    return f"{p.scheme}://{p.netloc}{path}"


def item_id(url: str) -> str | None:
    """The id in a page address such as /web/#/details?id=… (or the older /web/index.html#!/details?id=…)."""
    fragment = urlparse(url).fragment
    query = fragment.split("?", 1)[1] if "?" in fragment else ""
    return parse_qs(query).get("id", [None])[0]


def login_for(url: str, logins: dict) -> dict | None:
    """The login from jellyfin.json for the server url is on, or None if it isn't a Jellyfin server listed there."""
    server = server_of(url)
    return next((v for k, v in logins.items() if server_of(k) == server and isinstance(v, dict)), None)


def is_jellyfin(url: str) -> bool:
    """Whether url is on a Jellyfin server, for a helpful error when jellyfin.json has no login for it."""
    try:
        return "jellyfin" in request(server_of(url), "/System/Info/Public").get("ProductName", "").lower()
    except (OSError, ValueError, AttributeError):
        return False


def request(server: str, path: str, token: str | None = None, body: dict | None = None):
    auth = CLIENT + (f', Token="{token}"' if token else "")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": auth}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(server + path, json.dumps(body).encode() if body is not None else None, headers)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return json.loads(r.read(MAX_BYTES))


def log_in(server: str, login: dict) -> tuple[str, str]:
    try:
        data = request(server, "/Users/AuthenticateByName", body={
            "Username": login.get("username", ""), "Pw": login.get("password", "")})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise FeedError(f"Jellyfin at {server} didn't accept the username and password in jellyfin.json") from e
        raise
    tokens[server] = (data["AccessToken"], data["User"]["Id"])
    return tokens[server]


def get(server: str, login: dict, path: str, params: dict):
    """GET from the API as the user, logging in first, and again if the server has forgotten the token."""
    for attempt in range(2):
        token, user = tokens.get(server) or log_in(server, login)
        try:
            return request(server, f"{path}?{urlencode({**params, 'userId': user})}", token)
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt:
                raise
            tokens.pop(server, None)


def image(server: str, item: dict) -> str | None:
    """A wide picture for the card: the item's thumbnail or backdrop, or its poster, or its series'."""
    tags = item.get("ImageTags", {})
    for kind in ("Thumb", "Primary") if item["Type"] == "Episode" else ("Thumb", "Backdrop", "Primary"):
        tag = (item.get("BackdropImageTags") or [None])[0] if kind == "Backdrop" else tags.get(kind)
        if tag:
            return f"{server}/Items/{item['Id']}/Images/{kind}?maxWidth=640&tag={tag}"
    if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
        return f"{server}/Items/{item['SeriesId']}/Images/Primary?maxWidth=640&tag={item['SeriesPrimaryImageTag']}"
    return None


def title(item: dict) -> str:
    if item["Type"] == "Episode":
        number = f"S{item.get('ParentIndexNumber') or 0}E{item['IndexNumber']}" if item.get("IndexNumber") is not None else ""
        return " · ".join(x for x in (item.get("SeriesName"), number, item["Name"]) if x)
    return f"{item['Name']} ({item['ProductionYear']})" if item.get("ProductionYear") else item["Name"]


def post(server: str, item: dict) -> dict:
    added = item.get("DateCreated") or item.get("PremiereDate")
    p = {
        "url": f"{server}/web/#/details?id={quote(item['Id'])}&serverId={quote(item.get('ServerId', ''))}",
        "title": title(item)[:300],
        "description": (item.get("Overview") or "")[:500],
        "image": image(server, item),
        "youtube": None,
        "published": parse_date(added),
        "jellyfin": True,
        # A series' episodes follow each other in season and episode order; a movie is a series of its own.
        "series": item.get("SeriesId") or item["Id"],
    }
    if item["Type"] == "Episode":
        p["episode"] = [item.get("ParentIndexNumber") or 0, item.get("IndexNumber") or 0]
    return p


def parse_date(value: str | None) -> float | None:
    """Jellyfin's dates, such as 2024-01-15T20:56:11.0000000Z, which have more decimals than fromisoformat takes."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", value or "")
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else None


def icon(server: str) -> str | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(server + "/web/", headers={"User-Agent": USER_AGENT}),
                                    timeout=FETCH_TIMEOUT) as r:
            page = r.read(1_000_000).decode("utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'<link[^>]*rel="(?:shortcut )?icon"[^>]*href="([^"]+)"', page)
    return urljoin(server + "/web/", m.group(1)) if m else None


def fetch(url: str, login: dict) -> dict:
    """The movies and episodes on the server, or in the page url points at, newest added first, like feeds.fetch()."""
    server = server_of(url)
    parent = item_id(url)
    try:
        info = request(server, "/System/Info/Public")
        params = {"recursive": "true", "includeItemTypes": "Movie,Episode", "sortBy": "DateCreated",
                  "sortOrder": "Descending", "limit": MAX_ITEMS,
                  "fields": "Overview,DateCreated,PremiereDate,ProductionYear"}
        name = info.get("ServerName") or urlparse(server).netloc
        if parent:
            folder = get(server, login, f"/Items/{parent}", {})
            name = folder.get("Name") or name
            params["parentId"] = parent
        items = get(server, login, "/Items", params)["Items"]
    except urllib.error.HTTPError as e:
        raise FeedError(f"HTTP {e.code} from Jellyfin at {server}") from e
    except (OSError, ValueError, KeyError) as e:
        raise FeedError(f"couldn't read Jellyfin at {server}: {e}") from e
    return {
        "title": name,
        "site": server + "/web/",
        "icon": icon(server),
        "items": [post(server, it) for it in items if it.get("Type") in ("Movie", "Episode")],
        "fetched": time.time(),
        "feed_url": url,
        "complete": True,  # every post there is: ones missing from it were removed from the server
    }
