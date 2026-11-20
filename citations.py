"""Subscriptions to the papers citing a paper, as feed posts.

A subscription is the paper's address on arXiv (arxiv.org/abs/<id>, /pdf/<id> or /html/<id>) or on Google
Scholar: a paper on a profile (scholar.google.com/citations?view_op=view_citation&…), its "Cited by" list
(scholar.google.com/scholar?cites=<id>) or its versions (scholar.google.com/scholar?cluster=<id>).

The citing papers come from OpenAlex (api.openalex.org), a free catalog of scholarly works and the papers
they cite, newest first. Google Scholar has no API and turns away clients that ask often, so it is asked only
once, for the paper's title and year, which then find the paper on OpenAlex. An arXiv paper is found by its
arXiv DOI without asking arXiv. A subscription is checked at most every CHECK_INTERVAL seconds unless forced.
"""

import html
import json
import os
import re
import time
import urllib.error
from datetime import date, datetime
from urllib.parse import parse_qs, quote, urlencode, urlparse

from feeds import FeedError, arxiv_abs, open_url

OPENALEX = "https://api.openalex.org"
SCHOLAR = "https://scholar.google.com"
CHECK_INTERVAL = 6 * 3600
PAGE_SIZE = 50
MAX_BYTES = 4_000_000
FIELDS = "id,title,publication_date,authorships,primary_location,locations,ids"
ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"


def arxiv_id(url: str) -> str | None:
    """The arXiv id in a paper's address, without its version: "1706.03762" in arxiv.org/pdf/1706.03762v7."""
    p = urlparse(url)
    if p.netloc.lower().removeprefix("www.").removeprefix("export.") != "arxiv.org":
        return None
    m = re.fullmatch(rf"/(?:abs|pdf|html)/{ARXIV_ID}(?:v\d+)?(?:\.pdf)?/?", p.path)
    return m.group(1) if m else None


def scholar_paper(url: str) -> tuple[str, str] | None:
    """("citation", "<user>:<id>") for a paper on a Google Scholar profile, or ("cluster", "<id>") for a
    "Cited by" list or a paper's versions on Google Scholar."""
    p = urlparse(url)
    if not re.fullmatch(r"scholar\.google(\.com?)?(\.[a-z]{2})?", p.netloc.lower()):
        return None
    q = parse_qs(p.query)
    path = p.path.rstrip("/")
    if path == "/citations" and q.get("view_op", [""])[0] == "view_citation":
        cid = q.get("citation_for_view", [""])[0]
        return ("citation", cid) if re.fullmatch(r"[\w-]+:[\w-]+", cid) else None
    if path == "/scholar":
        cluster = q.get("cites", q.get("cluster", [""]))[0]
        return ("cluster", cluster) if re.fullmatch(r"\d+", cluster) else None
    return None


def is_paper(url: str) -> bool:
    return bool(arxiv_id(url) or scholar_paper(url))


def openalex(path: str, params: dict | None = None) -> dict:
    """A JSON response from OpenAlex. Without an OPENALEX_API_KEY, requests share a small free daily budget."""
    params = dict(params or {})
    if key := os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = key
    address = OPENALEX + path + ("?" + urlencode(params, safe=":|,") if params else "")
    try:
        with open_url(address, "application/json") as r:
            return json.loads(r.read(MAX_BYTES))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FeedError("OpenAlex doesn't list this paper yet; trying again later") from e
        if e.code == 429:
            raise FeedError("OpenAlex is turning away requests for now (HTTP 429); trying again later") from e
        raise FeedError(f"HTTP {e.code} from OpenAlex") from e
    except (OSError, ValueError) as e:
        raise FeedError(f"couldn't ask OpenAlex: {e}") from e


def text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def normalized(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


def scholar_page(address: str) -> str:
    try:
        with open_url(address, "text/html") as r:
            page = r.read(MAX_BYTES).decode("utf-8", errors="replace")
            final_url = r.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise FeedError("Google Scholar is turning away requests for now (HTTP 429); trying again later") from e
        raise FeedError(f"HTTP {e.code} from Google Scholar") from e
    except OSError as e:
        raise FeedError(f"couldn't fetch the paper from Google Scholar: {e}") from e
    if "/sorry/" in final_url or "gs_captcha" in page or "recaptcha" in page.lower():
        raise FeedError("Google Scholar asked for a CAPTCHA instead of showing the paper; trying again later")
    return page


def scholar_title(kind: str, ref: str) -> tuple[str, int | None]:
    """The (title, year) of a paper on Google Scholar, read from its page."""
    if kind == "citation":
        user = ref.split(":")[0]
        page = scholar_page(f"{SCHOLAR}/citations?view_op=view_citation&hl=en&user={user}&citation_for_view={ref}")
        title = re.search(r'<div id="gsc_oci_title"[^>]*>(.*?)</div>', page, re.S)
        when = re.search(r'gsc_oci_field">Publication date</div>\s*<div class="gsc_oci_value">\s*(\d{4})', page)
        title, year = title.group(1) if title else "", int(when.group(1)) if when else None
    else:
        page = scholar_page(f"{SCHOLAR}/scholar?cluster={ref}&hl=en")
        title = re.search(r'<h3 class="gs_rt"[^>]*>(.*?)</h3>', page, re.S)
        # The first version's title, without markers such as [PDF] and [CITATION]
        title = re.sub(r'<span class="gs_ct[^"]*"[^>]*>.*?</span>', "", title.group(1), flags=re.S) if title else ""
        byline = re.search(r'<div class="gs_a"[^>]*>(.*?)</div>', page, re.S)
        years = re.findall(r"\b(?:1[89]|20)\d\d\b", text(byline.group(1))) if byline else []
        year = int(years[-1]) if years else None
    if not text(title):
        raise FeedError("couldn't read the paper's title from Google Scholar: its page has changed")
    return text(title), year


def find_by_title(title: str, year: int | None) -> dict:
    """The OpenAlex work with this title (and about this year), the most cited one if several have it."""
    found = openalex("/works", {"filter": "title.search:" + title.replace(",", " ").replace("|", " "),
                                "per-page": 25, "select": "id,title,publication_year,cited_by_count"})
    wanted = normalized(title)
    matches = [w for w in found.get("results", []) if normalized(w.get("title") or "") == wanted
               and (year is None or w.get("publication_year") is None or abs(w["publication_year"] - year) <= 1)]
    if not matches:
        raise FeedError(f"couldn't find “{title}” on OpenAlex, which lists the papers citing it")
    return max(matches, key=lambda w: w.get("cited_by_count") or 0)


def resolve(url: str, known_titles: dict[str, str] | None = None) -> dict:
    """The cited paper: its OpenAlex id, title and address. known_titles: post url -> title of papers already
    in the feed, so a paper from a Google Scholar profile needn't be read from Google Scholar again."""
    if aid := arxiv_id(url):
        work = openalex(f"/works/doi:10.48550/arXiv.{aid}", {"select": "id,title"})
        link = f"https://arxiv.org/abs/{aid}"
    else:
        kind, ref = scholar_paper(url)
        title, year = None, None
        if kind == "citation":
            user, cid = ref.split(":")[0], ref
            title = (known_titles or {}).get(f"{SCHOLAR}/citations?view_op=view_citation&user={user}&citation_for_view={cid}")
        if not title:
            title, year = scholar_title(kind, ref)
        work = find_by_title(title, year)
        link = url
    return {"openalex": work["id"].rsplit("/", 1)[-1], "title": text(work.get("title") or "") or link, "url": link}


def paper_url(work: dict) -> str:
    """Where a citing paper's card links: its arXiv abstract when it has one, so it is the same post as in an
    arXiv author's feed, otherwise its DOI or its page on OpenAlex."""
    for location in work.get("locations") or []:
        link = arxiv_abs(location.get("landing_page_url") or "")
        if re.match(rf"^https://arxiv\.org/abs/{ARXIV_ID}$", link):
            return link
    doi = (work.get("ids") or {}).get("doi")
    if doi and doi.startswith("https://doi.org/10.48550/arxiv."):
        return "https://arxiv.org/abs/" + doi.removeprefix("https://doi.org/10.48550/arxiv.")
    return doi or (work.get("primary_location") or {}).get("landing_page_url") or work["id"]


def post(work: dict, now: float) -> dict:
    authors = [a.get("author", {}).get("display_name") for a in work.get("authorships") or []]
    authors = [a for a in authors if a]
    byline = ", ".join(authors[:6]) + (" et al." if len(authors) > 6 else "")
    venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    try:
        published = min(datetime.fromisoformat(work["publication_date"]).timestamp(), now)
    except (KeyError, TypeError, ValueError):
        published = None
    return {
        "url": paper_url(work),
        "title": (text(work.get("title") or "") or paper_url(work))[:300],
        "description": " · ".join(part for part in (byline, venue) if part)[:500],
        "image": None,
        "youtube": None,
        "estimated_minutes": None,
        "published": published,
    }


def fetch(url: str, cited: dict | None = None, known_titles: dict[str, str] | None = None) -> dict:
    """The newest papers citing the paper, like feeds.fetch(). cited: the paper, as resolve() found it before;
    known_titles: as for resolve()."""
    cited = cited or resolve(url, known_titles)
    today = date.today().isoformat()  # leave out papers dated in the future, such as a journal's next issue
    found = openalex("/works", {"filter": f"cites:{cited['openalex']},to_publication_date:{today}",
                                "sort": "publication_date:desc", "per-page": PAGE_SIZE, "select": FIELDS})
    now = time.time()
    return {
        "title": f"Citing “{cited['title']}”",
        "site": cited["url"],
        "icon": "https://arxiv.org/favicon.ico" if arxiv_id(url) else f"{SCHOLAR}/favicon.ico",
        "items": [post(w, now) for w in found.get("results", []) if w.get("id")],
        "fetched": now,
        "citations_checked": now,
        "cited": cited,
        "feed_url": f"{OPENALEX}/works?filter=cites:{quote(cited['openalex'])}&sort=publication_date:desc",
        "complete": False,  # older citing papers are still on OpenAlex
    }
