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
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from feeds import FETCH_TIMEOUT, FeedError

MAX_ITEMS = 5000  # most recently added first
MAX_BYTES = 50_000_000
STREAM_TIMEOUT = 60  # Jellyfin may take a while to start converting a video
# What a browser can play as it is; Jellyfin converts anything else to HLS with H.264 and AAC.
BROWSER = {
    "MaxStreamingBitrate": 20_000_000,
    "DirectPlayProfiles": [
        {"Type": "Video", "Container": "mp4,m4v", "VideoCodec": "h264", "AudioCodec": "aac,mp3"},
        {"Type": "Video", "Container": "webm", "VideoCodec": "vp8,vp9,av1", "AudioCodec": "vorbis,opus"},
    ],
    "TranscodingProfiles": [{"Type": "Video", "Context": "Streaming", "Protocol": "hls", "Container": "mp4",
                             "VideoCodec": "h264", "AudioCodec": "aac", "MaxAudioChannels": "2", "MinSegments": 1,
                             "BreakOnNonKeyFrames": True}],
    "CodecProfiles": [], "ContainerProfiles": [], "SubtitleProfiles": [],
}
USER_AGENT = "ContentFeed/1.0"  # an API client's; some proxies, such as the demo server's, refuse browser-like ones
CLIENT = 'MediaBrowser Client="ContentFeed", Device="ContentFeed", DeviceId="contentfeed", Version="1.0"'

tokens: dict[str, tuple[str, str]] = {}  # server -> (access token, user id), kept in memory only


def server_of(url: str) -> str:
    """The server's base address: everything before /web, without a trailing slash."""
    p = urlparse(url)
    path = re.split(r"/web(?:/|$)", p.path, maxsplit=1)[0].rstrip("/")
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"


def host_of(address: str) -> str | None:
    """The host name in an address, even one written without http:// in front."""
    try:
        return urlparse(address if "://" in address else "//" + address).hostname
    except ValueError:
        return None


def item_id(url: str) -> str | None:
    """The id in a page address such as /web/#/details?id=… (or the older /web/index.html#!/details?id=…)."""
    fragment = urlparse(url).fragment
    query = fragment.split("?", 1)[1] if "?" in fragment else ""
    return parse_qs(query).get("id", [None])[0]


def login_for(url: str, logins: dict) -> dict | None:
    """The login from jellyfin.json for the server url is on, or None if it isn't a Jellyfin server listed there."""
    server = server_of(url)
    return next((v for k, v in logins.items() if server_of(k) == server and isinstance(v, dict)), None)


def read_logins(path: Path) -> tuple[dict, str | None]:
    """The logins in jellyfin.json, and what's wrong with the file if it can't be used."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, f"there's no {path}"
    except (OSError, UnicodeDecodeError) as e:
        return {}, f"couldn't read {path}: {getattr(e, 'strerror', None) or e}"
    if not text.strip():
        return {}, f"{path} is empty"
    try:
        logins = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, f"{path} isn't valid JSON: {e.msg.lower()} at line {e.lineno}, column {e.colno}"
    if not isinstance(logins, dict):
        return {}, f"{path} should hold an object of server addresses, not a {type(logins).__name__}"
    return logins, None


def missing_login(url: str, logins: dict, problem: str | None, path: Path) -> str:
    """Why there's no usable login for the server url is on, and what to add."""
    server = server_of(url)
    example = json.dumps({server: {"username": "…", "password": "…"}}, ensure_ascii=False)
    matching = [k for k in logins if server_of(k) == server]
    same_host = [k for k in logins if host_of(k) == host_of(server)]
    if problem:
        why = problem
    elif matching:
        why = (f"the login for {matching[0]} in {path} should be an object like "
               '{"username": "…", "password": "…"}')
    elif not logins:
        why = f"{path} has no logins"
    else:
        why = f"{path} has logins only for {', '.join(sorted(logins))}"
        if same_host:
            why += (f". {same_host[0]} is on the same host, but the address must match, including "
                    "http:// or https:// and the port")
    return f"{server} is a Jellyfin server, but there's no login for it: {why}. Add one like {example}"


def is_jellyfin(url: str) -> bool:
    """Whether url is on a Jellyfin server, for a helpful error when jellyfin.json has no login for it."""
    try:
        return "jellyfin" in request(server_of(url), "/System/Info/Public").get("ProductName", "").lower()
    except (OSError, ValueError, AttributeError):
        return False


def open_url(server: str, path: str, token: str | None = None, body: dict | None = None, headers: dict | None = None,
             timeout: float = FETCH_TIMEOUT):
    auth = CLIENT + (f', Token="{token}"' if token else "")
    h = {"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": auth, **(headers or {})}
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(server + path, json.dumps(body).encode() if body is not None else None, h)
    return urllib.request.urlopen(req, timeout=timeout)


def request(server: str, path: str, token: str | None = None, body: dict | None = None):
    with open_url(server, path, token, body) as r:
        return json.loads(r.read(MAX_BYTES))


def log_in(server: str, login: dict) -> tuple[str, str]:
    try:
        data = request(server, "/Users/AuthenticateByName", body={
            "Username": login.get("username", ""), "Pw": login.get("password", "")})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise FeedError(f"Jellyfin at {server} didn't accept the password for user "
                            f"\"{login.get('username', '')}\" in jellyfin.json") from e
        raise
    tokens[server] = (data["AccessToken"], data["User"]["Id"])
    return tokens[server]


def as_user(server: str, login: dict, call):
    """call(token, user id), logging in first, and again if the server has forgotten the token."""
    for attempt in range(2):
        token, user = tokens.get(server) or log_in(server, login)
        try:
            return call(token, user)
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt:
                raise
            tokens.pop(server, None)


def get(server: str, login: dict, path: str, params: dict, body: dict | None = None):
    """Call the API as the user: GET, or POST with a body."""
    return as_user(server, login, lambda token, user: request(
        server, f"{path}?{urlencode({**params, 'userId': user})}", token, body))


def image(server: str, item: dict) -> str | None:
    """A wide picture for the card: the item's thumbnail or backdrop, or its poster, or its series' backdrop or poster."""
    tags = item.get("ImageTags", {})
    for kind in ("Thumb", "Primary") if item["Type"] == "Episode" else ("Thumb", "Backdrop", "Primary"):
        tag = (item.get("BackdropImageTags") or [None])[0] if kind == "Backdrop" else tags.get(kind)
        if tag:
            return f"{server}/Items/{item['Id']}/Images/{kind}?maxWidth=640&tag={tag}"
    if item.get("ParentBackdropItemId") and item.get("ParentBackdropImageTags"):  # the series' backdrop
        return (f"{server}/Items/{item['ParentBackdropItemId']}/Images/Backdrop?maxWidth=640"
                f"&tag={item['ParentBackdropImageTags'][0]}")
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


# Playing in the feed. The browser gets videos through the feed server, which adds the token, so the token
# and password never reach it. Jellyfin puts the token in the addresses it returns, so it is taken out.

TOKEN_PARAM = re.compile(r"(?i)(?<=[?&])api_?key=[^&\"\s]*&?")


def without_token(text: str) -> str:
    return TOKEN_PARAM.sub("", text)


def normal_id(item: str) -> str:
    """Jellyfin writes ids both as 32 hex digits and as a GUID with dashes."""
    return item.replace("-", "").lower()


def playback(url: str, login: dict) -> tuple[str, bool]:
    """How a browser can play the item at url: (path on its server, without the token; whether it's HLS)."""
    server, item = server_of(url), item_id(url)
    info = get(server, login, f"/Items/{quote(item)}/PlaybackInfo", {}, {"DeviceProfile": BROWSER})
    sources = info.get("MediaSources") or []
    if not sources:
        raise FeedError("Jellyfin has nothing to play for this item")
    source = sources[0]
    if source.get("SupportsDirectPlay"):
        query = urlencode({"static": "true", "mediaSourceId": source["Id"], "playSessionId": info.get("PlaySessionId", "")})
        return f"/videos/{normal_id(item)}/stream?{query}", False
    if not source.get("TranscodingUrl"):
        raise FeedError("Jellyfin can't convert this item for the browser")
    return without_token(source["TranscodingUrl"]), True


def stream(server: str, login: dict, path: str, headers: dict):
    """Open a video, playlist or segment as the user; the caller closes it."""
    return as_user(server, login, lambda token, user: open_url(server, path, token, headers=headers, timeout=STREAM_TIMEOUT))
