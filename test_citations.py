"""Citation subscriptions: the papers citing a paper on arXiv or Google Scholar, from OpenAlex."""
import json
import time
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import citations
import feeds
import scholar
import server

NOW = datetime(2026, 11, 20, 12).timestamp()
PROFILE_PAPER = ("https://scholar.google.com/citations?view_op=view_citation&user=abcDEF12AAAJ"
                 "&citation_for_view=abcDEF12AAAJ:u5HHmVD_uO8C")

CITING = {"results": [
    {"id": "https://openalex.org/W1", "title": "Newer <i>work</i>", "publication_date": "2026-11-01",
     "authorships": [{"author": {"display_name": f"Author {i}"}} for i in range(8)],
     "primary_location": {"landing_page_url": "https://doi.org/10.1/x", "source": {"display_name": "A Journal"}},
     "locations": [{"landing_page_url": "https://doi.org/10.1/x"}, {"landing_page_url": "http://arxiv.org/abs/2611.00001v2"}],
     "ids": {"doi": "https://doi.org/10.1/x"}},
    {"id": "https://openalex.org/W2", "title": "Only a DOI", "publication_date": "2026-12-31",
     "authorships": [{"author": {"display_name": "Solo"}}], "primary_location": None, "locations": [],
     "ids": {"doi": "https://doi.org/10.2/y"}},
    {"id": "https://openalex.org/W3", "title": "arXiv DOI", "publication_date": None, "authorships": [],
     "primary_location": None, "locations": [], "ids": {"doi": "https://doi.org/10.48550/arxiv.2610.00002"}},
]}


class FakeResponse:
    def __init__(self, body, url=""):
        self.body = body if isinstance(body, bytes) else body.encode()
        self.url = url

    def read(self, size=-1):
        return self.body

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWeb:
    """Answers OpenAlex and Google Scholar requests from a table, recording what was asked."""

    def __init__(self, scholar_page=""):
        self.asked, self.scholar_page = [], scholar_page

    def __call__(self, url, accept, headers=None):
        self.asked.append(url)
        p = urlparse(url)
        if p.netloc == "scholar.google.com":
            return FakeResponse(self.scholar_page, url)
        q = parse_qs(p.query)
        if p.path.startswith("/works/doi:"):
            return FakeResponse(json.dumps({"id": "https://openalex.org/W99", "title": "Attention Is All You Need"}))
        if q["filter"][0].startswith("title.search:"):
            return FakeResponse(json.dumps({"results": [
                {"id": "https://openalex.org/W7", "title": "Attention is all you need", "publication_year": 2017, "cited_by_count": 9},
                {"id": "https://openalex.org/W8", "title": "Attention Is All You Need!", "publication_year": 2017, "cited_by_count": 99},
                {"id": "https://openalex.org/W9", "title": "Attention Is All You Need", "publication_year": 2025, "cited_by_count": 999},
                {"id": "https://openalex.org/W6", "title": "Attention Is All You Need in Speech", "publication_year": 2017, "cited_by_count": 9999},
            ]}))
        return FakeResponse(json.dumps(CITING))


class AddressTests(unittest.TestCase):
    def test_arxiv_ids_without_versions(self):
        for address, aid in (("https://arxiv.org/abs/1706.03762", "1706.03762"),
                             ("https://arxiv.org/abs/1706.03762v7", "1706.03762"),
                             ("https://arxiv.org/pdf/1706.03762v2.pdf", "1706.03762"),
                             ("https://www.arxiv.org/html/2401.12345v1", "2401.12345"),
                             ("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001")):
            with self.subTest(address=address):
                self.assertEqual(citations.arxiv_id(address), aid)
        self.assertIsNone(citations.arxiv_id("https://arxiv.org/a/lecun_y_1"))
        self.assertIsNone(citations.arxiv_id("https://example.com/abs/1706.03762"))

    def test_scholar_papers(self):
        self.assertEqual(citations.scholar_paper(PROFILE_PAPER), ("citation", "abcDEF12AAAJ:u5HHmVD_uO8C"))
        self.assertEqual(citations.scholar_paper("https://scholar.google.no/scholar?oi=bibs&hl=en&cites=5232365541765765095"),
                         ("cluster", "5232365541765765095"))
        self.assertEqual(citations.scholar_paper("https://scholar.google.com/scholar?cluster=123&hl=en"), ("cluster", "123"))
        self.assertIsNone(citations.scholar_paper("https://scholar.google.com/citations?user=abcDEF12AAAJ"))
        self.assertIsNone(citations.scholar_paper("https://scholar.google.com/scholar?q=attention"))

    def test_a_paper_on_a_profile_is_not_the_author(self):
        self.assertIsNone(scholar.user_id(PROFILE_PAPER))
        self.assertEqual(scholar.user_id("https://scholar.google.com/citations?user=abcDEF12AAAJ&view_op=list_works"),
                         "abcDEF12AAAJ")


class FetchTests(unittest.TestCase):
    def fetch(self, url, web, **kw):
        with patch.object(citations, "open_url", web), patch.object(citations.time, "time", return_value=NOW):
            return citations.fetch(url, **kw)

    def test_arxiv_paper_is_found_by_its_doi_and_citing_papers_listed_newest_first(self):
        web = FakeWeb()
        feed = self.fetch("https://arxiv.org/pdf/1706.03762v7", web)
        self.assertIn("/works/doi:10.48550/arXiv.1706.03762", web.asked[0])
        query = parse_qs(urlparse(web.asked[1]).query)
        self.assertTrue(query["filter"][0].startswith("cites:W99,to_publication_date:"))
        self.assertEqual(query["sort"], ["publication_date:desc"])
        self.assertEqual(feed["title"], "Citing “Attention Is All You Need”")
        self.assertEqual(feed["cited"], {"openalex": "W99", "title": "Attention Is All You Need",
                                         "url": "https://arxiv.org/abs/1706.03762"})
        posts = feed["items"]
        # An arXiv version is preferred, so the paper is the same post as in an arXiv author's feed
        self.assertEqual([p["url"] for p in posts], ["https://arxiv.org/abs/2611.00001", "https://doi.org/10.2/y",
                                                     "https://arxiv.org/abs/2610.00002"])
        self.assertEqual(posts[0]["title"], "Newer work")
        self.assertEqual(posts[0]["description"], "Author 0, Author 1, Author 2, Author 3, Author 4, Author 5 et al. · A Journal")
        self.assertEqual(posts[0]["published"], datetime(2026, 11, 1).timestamp())
        self.assertEqual(posts[1]["published"], NOW)  # dated in the future
        self.assertIsNone(posts[2]["published"])
        self.assertEqual(feed["citations_checked"], NOW)

    def test_known_paper_is_not_looked_up_again(self):
        web = FakeWeb()
        cited = {"openalex": "W5", "title": "Known", "url": "https://arxiv.org/abs/1706.03762"}
        feed = self.fetch("https://arxiv.org/abs/1706.03762", web, cited=cited)
        self.assertEqual(len(web.asked), 1)
        self.assertIn("cites:W5", web.asked[0])
        self.assertEqual(feed["title"], "Citing “Known”")

    def test_paper_from_a_followed_profile_uses_its_title_without_asking_scholar(self):
        web = FakeWeb()
        known = {PROFILE_PAPER: "Attention is all you need"}
        feed = self.fetch(PROFILE_PAPER + "&hl=en", web, known_titles=known)
        self.assertFalse(any("scholar.google" in u for u in web.asked))
        self.assertEqual(feed["cited"]["openalex"], "W9")  # same title, most cited; the year isn't known
        self.assertEqual(feed["cited"]["url"], PROFILE_PAPER + "&hl=en")

    def test_profile_paper_page_gives_title_and_year(self):
        page = ('<div id="gsc_oci_title"><a class="gsc_oci_title_link" href="x">Attention is all you need</a></div>'
                '<div class="gs_scl"><div class="gsc_oci_field">Publication date</div><div class="gsc_oci_value">2017/6/12</div></div>')
        web = FakeWeb(page)
        feed = self.fetch(PROFILE_PAPER, web)
        self.assertIn("scholar.google.com/citations?view_op=view_citation", web.asked[0])
        self.assertEqual(feed["cited"]["openalex"], "W8")  # the 2025 paper of that name is another one

    def test_cited_by_list_reads_the_papers_versions(self):
        page = ('<h3 class="gs_rt"><span class="gs_ctg2">[PDF]</span> <a href="x">Attention is <b>all</b> you need</a></h3>'
                '<div class="gs_a">A Vaswani, N Shazeer - Advances in neural information processing systems, 2017 - proceedings.neurips.cc</div>')
        web = FakeWeb(page)
        feed = self.fetch("https://scholar.google.com/scholar?cites=5232365541765765095&as_sdt=2005", web)
        self.assertIn("scholar.google.com/scholar?cluster=5232365541765765095", web.asked[0])
        self.assertEqual(feed["cited"]["openalex"], "W8")
        self.assertEqual(feed["icon"], "https://scholar.google.com/favicon.ico")

    def test_unknown_title_and_captcha_are_errors(self):
        with self.assertRaisesRegex(feeds.FeedError, "couldn't find “Something else”"):
            self.fetch(PROFILE_PAPER, FakeWeb(), known_titles={PROFILE_PAPER: "Something else"})
        with self.assertRaisesRegex(feeds.FeedError, "CAPTCHA"):
            self.fetch(PROFILE_PAPER, FakeWeb("<div id='gs_captcha_f'>"))


class ServerTests(unittest.TestCase):
    address = "https://arxiv.org/abs/1706.03762"

    def test_detected_and_known_before_profiles_and_feeds(self):
        for address in (self.address, PROFILE_PAPER):
            with self.subTest(address=address):
                self.assertEqual(server.known_source(address, {}), "citations")
                with patch.object(citations, "fetch", return_value={"items": []}) as fetch:
                    self.assertEqual(server.detect_source(address)[0], "citations")
                self.assertEqual(fetch.call_args.args[:2], (address, None))

    def test_checked_at_most_every_interval_unless_forced(self):
        cited = {"openalex": "W5", "title": "Known", "url": self.address}
        old = {"citations_checked": time.time() - 60, "cited": cited, "items": []}
        with patch.object(citations, "fetch", return_value={"items": []}) as fetch:
            self.assertEqual(server.fetch_source("citations", self.address, old, False), ("citations", None))
            fetch.assert_not_called()
            server.fetch_source("citations", self.address, old, True)
            fetch.assert_called_once_with(self.address, cited, None)

    def test_cards_say_which_author_and_which_cited_paper(self):
        paper = {"url": "https://arxiv.org/abs/2611.00001", "title": "P", "published": NOW, "first_seen": NOW}
        other = {**paper, "url": "https://doi.org/10.2/y"}
        subs = {
            "https://arxiv.org/a/lecun_y_1": {"source": "feed", "feed_url": "https://arxiv.org/a/lecun_y_1.atom2",
                                              "title": "Yann LeCun's articles on arXiv", "items": [paper]},
            "https://arxiv.org/search/?query=Doe&searchtype=author": {
                "source": "feed", "title": "Doe, Jane on arXiv", "items": [paper],
                "feed_url": "https://export.arxiv.org/api/query?search_query=au%3A%22Doe%22"},
            self.address: {"source": "citations", "title": "Citing “Attention”", "items": [paper, other],
                           "cited": {"openalex": "W5", "title": "Attention", "url": self.address}},
            "https://example.com/feed": {"source": "feed", "feed_url": "https://example.com/feed", "title": "Blog",
                                         "items": [{**paper, "url": "https://example.com/post"}]},
        }
        with patch.object(server, "subscriptions", subs), patch.object(server, "added", {}):
            items, _ = server.build_items({}, {url: [] for url in subs}, NOW)
        via = {it["url"]: [(r["kind"], r["name"]) for r in it["via"]] for it in items}
        self.assertEqual(via[paper["url"]], [("author", "Yann LeCun"), ("author", "Doe, Jane"), ("cites", "Attention")])
        self.assertEqual(via[other["url"]], [("cites", "Attention")])
        self.assertEqual(via["https://example.com/post"], [])
        self.assertTrue(server.matches_search(next(it for it in items if it["url"] == other["url"]), "attention"))


if __name__ == "__main__":
    unittest.main()
