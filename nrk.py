"""Subscriptions to series on NRK TV (tv.nrk.no): their episodes, as feed posts.

A subscription is a series' page, such as https://tv.nrk.no/serie/skam, or any page under it, such as
one of its episodes. Episodes come from NRK's public catalogue API (psapi.nrk.no), which needs no login.
It isn't documented, so it may change.

A series with seasons ("sequential", like a drama) lists every season's episodes in one response, and
they go in season and episode order. Other series, such as news and talk shows, list their episodes
newest first, a month or a year to a page; pages are read until one has an episode the feed already
has, so after the first check one or two requests are enough.
"""

import json
import re
import time
import urllib.error
from datetime import datetime
from urllib.parse import quote, urlparse

from feeds import MAX_FEED_BYTES, FeedError, open_url

API = "https://psapi.nrk.no"
SITE = "https://tv.nrk.no"
MAX_ITEMS = 1000  # for series without seasons, newest first
IMAGE_WIDTH = 960


def series_id(url: str) -> str | None:
    """The series' id in a tv.nrk.no address, such as "skam" in https://tv.nrk.no/serie/skam/sesong/1."""
    p = urlparse(url)
    if p.netloc.lower().removeprefix("www.") != "tv.nrk.no":
        return None
    m = re.match(r"^/serie/([\w-]+)", p.path)
    return m.group(1) if m else None


def request(path: str) -> dict:
    with open_url(API + path, "application/json") as r:
        return json.loads(r.read(MAX_FEED_BYTES))


def image(images: list | None) -> str | None:
    """The smallest of NRK's sizes that is at least IMAGE_WIDTH wide, or else the largest."""
    images = sorted(images or [], key=lambda im: im.get("width", 0))
    return next((im["url"] for im in images if im.get("width", 0) >= IMAGE_WIDTH), images[-1]["url"] if images else None)


def parse_date(value: str | None) -> datetime | None:
    """NRK's dates, such as 2015-09-25T20:30:00+02:00, in Norwegian time."""
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def last_part(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "desember"]
# Episodes of the last week are titled by day, such as "I dag · …" or "Fredag · …"; older ones by date.
RELATIVE_DAY = re.compile(r"^(I dag|I går|Mandag|Tirsdag|Onsdag|Torsdag|Fredag|Lørdag|Søndag)\b")


def dated(name: str, day: datetime | None) -> str:
    """The title with a day such as "I går" replaced by its date, so it stays right."""
    return RELATIVE_DAY.sub(f"{day.day}. {MONTHS[day.month - 1]}", name) if day else name


def post(series: str, season: str, episode: dict, number: list[int] | None) -> dict:
    titles = episode.get("titles") or {}
    published = parse_date(episode.get("releaseDateOnDemand") or episode.get("usageRights", {}).get("from", {}).get("date"))
    name = dated(titles.get("title") or episode["prfId"], published)
    p = {
        "url": f"{SITE}/serie/{series}/sesong/{quote(season)}/episode/{quote(episode['prfId'])}",
        "title": (f"S{number[0]}E{number[1]} · {name}" if number else name)[:300],
        "description": (titles.get("subtitle") or "")[:500],
        "image": image(episode.get("image")),
        "youtube": None,
        "published": published.timestamp() if published else None,
    }
    if number:  # episodes follow each other in season and episode order
        p["series"], p["episode"] = series, number
    else:  # dated episodes, such as the news; "5. oktober" doesn't follow "4. oktober" as a part of a series
        p["no_series"] = True
    return p


def available(episode: dict) -> bool:
    """Whether it can be watched now: not one that's coming later, or no longer available."""
    return episode.get("availability", {}).get("status", "available") == "available" and bool(episode.get("prfId"))


def fetch(url: str, known: set[str] = frozenset()) -> dict:
    """The series' episodes that can be watched, like feeds.fetch(). known: urls the feed already has."""
    series = series_id(url)
    try:
        data = request(f"/tv/catalog/series/{quote(series)}")
        embedded = data.get("_embedded", {})
        info = data.get(data.get("seriesType")) or {}  # such as data["sequential"], with its titles
        if data.get("seriesType") == "sequential":
            items = [
                post(series, last_part(season["_links"]["self"]["href"]), ep,
                     [season.get("sequenceNumber") or 0, ep.get("sequenceNumber") or 0])
                for season in embedded.get("seasons", [])
                for ep in season.get("_embedded", {}).get("episodes", []) if available(ep)
            ]
            complete = True
        else:
            items, page = [], embedded.get("instalments", {})
            while page:
                new = [post(series, ep.get("_links", {}).get("season", {}).get("name", ""), ep, None)
                       for ep in page.get("_embedded", {}).get("instalments", []) if available(ep)]
                items += new
                nxt = page.get("_links", {}).get("next", {}).get("href")
                if not nxt or len(items) >= MAX_ITEMS or any(p["url"] in known for p in new):
                    break
                page = request(nxt)
            complete = False  # older episodes than were read are still there
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FeedError(f"NRK TV has no series \"{series}\"") from e
        raise FeedError(f"HTTP {e.code} from NRK TV") from e
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise FeedError(f"couldn't read NRK TV's series \"{series}\": {e}") from e
    items.sort(key=lambda p: p["published"] or 0, reverse=True)
    return {
        "title": (info.get("titles") or {}).get("title") or series,
        "site": f"{SITE}/serie/{series}",
        "icon": f"{SITE}/favicon.ico",
        "items": items[:MAX_ITEMS],
        "fetched": time.time(),
        "feed_url": url,
        "complete": complete,  # for a series with seasons: episodes missing from it are no longer available
    }
