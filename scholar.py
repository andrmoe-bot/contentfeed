"""Subscriptions to authors on Google Scholar: the papers on their profile, as feed posts.

A subscription is a profile's address, such as https://scholar.google.com/citations?user=JicYPdAAAAAJ.
Google Scholar has no feed or API, so the papers are read from the profile page, newest first, 100 at a
time. The page gives only each paper's year, so its date is approximate: a paper from this year or last
year that turns up after the first check is dated when it was found, and other papers in the middle of
their year (or, this year, halfway to today), in the profile's order.

Google Scholar turns away clients that ask often, so a profile is checked at most every CHECK_INTERVAL
seconds, and each paper's own page is never read. When it asks for a CAPTCHA instead of the profile, the
Subscriptions page shows the error and the next check tries again.
"""

import html
import re
import time
import urllib.error
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from feeds import FeedError, open_url

SITE = "https://scholar.google.com"
PAGE_SIZE = 100  # the most Google Scholar shows on one page
CHECK_INTERVAL = 6 * 3600
MAX_PAGE_BYTES = 4_000_000

ROW = re.compile(r'<tr class="gsc_a_tr">(.*?)</tr>', re.S)
TITLE = re.compile(r'<a href="([^"]*citation_for_view=([^"&]+)[^"]*)" class="gsc_a_at">(.*?)</a>', re.S)
GRAY = re.compile(r'<div class="gs_gray">(.*?)</div>', re.S)
YEAR = re.compile(r'class="gsc_a_y"><span[^>]*>(\d{4})')
NAME = re.compile(r'<div id="gsc_prf_in">(.*?)</div>', re.S)
PHOTO = re.compile(r'<img[^>]*\bid="gsc_prf_pup-img"[^>]*>')
SRC = re.compile(r'\bsrc="([^"]+)"')


def user_id(url: str) -> str | None:
    """The profile's id in a Google Scholar address, such as "JicYPdAAAAAJ" in .../citations?user=JicYPdAAAAAJ."""
    p = urlparse(url)
    host = p.netloc.lower()
    if not re.fullmatch(r"scholar\.google(\.com?)?(\.[a-z]{2})?", host) or p.path.rstrip("/") != "/citations":
        return None
    q = parse_qs(p.query)
    if q.get("view_op", [""])[0] not in ("", "list_works"):
        return None  # a single paper on the profile, which is a subscription to the papers citing it
    user = q.get("user", [""])[0]
    return user if re.fullmatch(r"[\w-]+", user) else None


def text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def papers_on_page(page: str, user: str) -> list[dict]:
    """The profile page's papers in its order, each with url, title, authors, venue and year (or None)."""
    papers = []
    for row in ROW.findall(page):
        title = TITLE.search(row)
        if not title:
            continue
        gray = [text(g) for g in GRAY.findall(row)]
        year = YEAR.search(row)
        papers.append({
            "url": f"{SITE}/citations?view_op=view_citation&user={user}&citation_for_view={html.unescape(title.group(2))}",
            "title": text(title.group(3)),
            "authors": gray[0] if gray else "",
            # The venue ends with ", 2024", which is shown as the date instead
            "venue": re.sub(r",?\s*\d{4}$", "", gray[1]) if len(gray) > 1 else "",
            "year": int(year.group(1)) if year else None,
        })
    return papers


def year_date(year: int | None, now: float) -> float | None:
    """The middle of the year, or for this year halfway from its start to now."""
    if year is None:
        return None
    start = datetime(year, 1, 1).timestamp()
    middle = datetime(year, 7, 1).timestamp()
    return min(middle, (start + now) / 2)


def dated(papers: list[dict], known: dict[str, float | None], now: float) -> list[dict]:
    """Feed posts for the papers. Known papers keep their date (known: url -> published); new ones on a
    profile already checked were published lately, so they get now; the rest, the middle of their year."""
    posts, previous = [], now + 1
    for paper in papers:
        if paper["url"] in known:
            published = known[paper["url"]]
        elif known and paper["year"] and paper["year"] >= datetime.fromtimestamp(now).year - 1:
            published = now
        else:
            # Keep the profile's newest-first order where several papers share a year
            published = year_date(paper["year"], now)
            published = min(published, previous - 1) if published is not None else None
        previous = published if published is not None else previous
        posts.append({
            "url": paper["url"],
            "title": (paper["title"] or paper["url"])[:300],
            "description": " · ".join(part for part in (paper["authors"], paper["venue"]) if part)[:500],
            "image": None,
            "youtube": None,
            "estimated_minutes": None,
            "published": published,
            "date_approx": True,
            "scholar": True,
        })
    return posts


def fetch(url: str, known: dict[str, float | None] | None = None) -> dict:
    """The profile's latest papers, like feeds.fetch(). known: url -> published of posts the feed has."""
    user = user_id(url)
    address = f"{SITE}/citations?user={user}&hl=en&view_op=list_works&sortby=pubdate&cstart=0&pagesize={PAGE_SIZE}"
    try:
        with open_url(address, "text/html") as r:
            page = r.read(MAX_PAGE_BYTES).decode("utf-8", errors="replace")
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FeedError(f"Google Scholar has no profile \"{user}\"") from e
        if e.code == 429:
            raise FeedError("Google Scholar is turning away requests for now (HTTP 429); trying again later") from e
        raise FeedError(f"HTTP {e.code} from Google Scholar") from e
    except OSError as e:
        raise FeedError(f"couldn't fetch the Google Scholar profile: {e}") from e
    if "/sorry/" in final_url or "gs_captcha" in page or "recaptcha" in page.lower():
        raise FeedError("Google Scholar asked for a CAPTCHA instead of showing the profile; trying again later")
    if 'id="gsc_a_b"' not in page:
        raise FeedError("couldn't read the Google Scholar profile: its page has changed")
    now = time.time()
    name = NAME.search(page)
    photo = SRC.search(img.group(0)) if (img := PHOTO.search(page)) else None
    return {
        "title": text(name.group(1)) if name else user,
        "site": f"{SITE}/citations?user={user}",
        "icon": html.unescape(photo.group(1)) if photo else f"{SITE}/favicon.ico",
        "items": dated(papers_on_page(page, user), known or {}, now),
        "fetched": now,
        "scholar_checked": now,
        "feed_url": url,
        "complete": False,  # older papers than the first page are still on the profile
    }
