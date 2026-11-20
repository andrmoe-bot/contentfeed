"""Author subscriptions: arXiv author pages and searches, and Google Scholar profiles."""
import time
import unittest
from datetime import datetime
from unittest.mock import patch

import feeds
import scholar
import server

PROFILE = """<html><body>
<img alt="A Researcher" src="https://scholar.googleusercontent.com/citations?view_op=view_photo&amp;user=abcDEF12AAAJ&amp;citpid=2" id="gsc_prf_pup-img">
<div id="gsc_prf_in">A Researcher</div>
<table><tbody id="gsc_a_b">
<tr class="gsc_a_tr"><td class="gsc_a_t"><a href="/citations?view_op=view_citation&amp;hl=en&amp;user=abcDEF12AAAJ&amp;sortby=pubdate&amp;citation_for_view=abcDEF12AAAJ:newer" class="gsc_a_at">Newer &amp; better</a><div class="gs_gray">A Researcher, B Other</div><div class="gs_gray">Journal 3 (1), 20<span class="gs_oph">, 2026</span></div></td><td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">2026</span></td></tr>
<tr class="gsc_a_tr"><td class="gsc_a_t"><a href="/citations?view_op=view_citation&amp;hl=en&amp;user=abcDEF12AAAJ&amp;sortby=pubdate&amp;citation_for_view=abcDEF12AAAJ:older" class="gsc_a_at">Older</a><div class="gs_gray">A Researcher</div><div class="gs_gray">Conference<span class="gs_oph">, 2026</span></div></td><td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">2026</span></td></tr>
<tr class="gsc_a_tr"><td class="gsc_a_t"><a href="/citations?view_op=view_citation&amp;hl=en&amp;user=abcDEF12AAAJ&amp;sortby=pubdate&amp;citation_for_view=abcDEF12AAAJ:old" class="gsc_a_at">Old</a><div class="gs_gray">A Researcher</div><div class="gs_gray"></div></td><td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">2019</span></td></tr>
</tbody></table></body></html>"""

NOW = datetime(2026, 11, 20, 12).timestamp()


class FakeResponse:
    def __init__(self, body, url="https://scholar.google.com/citations?user=abcDEF12AAAJ"):
        self.body, self.url = body.encode(), url

    def read(self, size=-1):
        return self.body

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def url(cid):
    return f"https://scholar.google.com/citations?view_op=view_citation&user=abcDEF12AAAJ&citation_for_view=abcDEF12AAAJ:{cid}"


class ArxivTests(unittest.TestCase):
    def test_author_page_becomes_its_feed(self):
        for address in ("https://arxiv.org/a/lecun_y_1", "https://arxiv.org/a/lecun_y_1.html",
                        "https://www.arxiv.org/a/lecun_y_1.atom"):
            with self.subTest(address=address):
                self.assertEqual(feeds.known_feed_url(address), "https://arxiv.org/a/lecun_y_1.atom2")

    def test_author_search_becomes_api_query_newest_first(self):
        feed = feeds.known_feed_url("https://arxiv.org/search/?query=LeCun%2C+Yann&searchtype=author&size=50")
        self.assertEqual(feed, "https://export.arxiv.org/api/query?search_query=au%3A%22LeCun%2C%20Yann%22"
                               "&sortBy=submittedDate&sortOrder=descending&max_results=50")
        self.assertIsNone(feeds.known_feed_url("https://arxiv.org/search/?query=transformers&searchtype=all"))
        self.assertIsNone(feeds.known_feed_url("https://arxiv.org/abs/2401.00001"))

    def test_api_feed_title_and_links_without_versions(self):
        body = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <title>arXiv Query: search_query=au:"LeCun, Yann"&amp;id_list=&amp;start=0&amp;max_results=50</title>
          <entry><id>http://arxiv.org/abs/2401.00001v3</id><title>A paper</title>
            <published>2024-01-01T00:00:00Z</published><summary>About it.</summary>
            <link href="http://arxiv.org/abs/2401.00001v3" rel="alternate" type="text/html"/></entry>
          <entry><id>http://arxiv.org/abs/hep-th/9901001v1</id><title>Old style</title>
            <link href="https://arxiv.org/abs/hep-th/9901001v1" rel="alternate"/></entry>
        </feed>"""
        feed = feeds.parse(body, "https://export.arxiv.org/api/query")
        self.assertEqual(feed["title"], "LeCun, Yann on arXiv")
        self.assertEqual([p["url"] for p in feed["items"]],
                         ["https://arxiv.org/abs/2401.00001", "https://arxiv.org/abs/hep-th/9901001"])


class ScholarTests(unittest.TestCase):
    def test_user_id_from_profile_addresses(self):
        self.assertEqual(scholar.user_id("https://scholar.google.com/citations?user=abcDEF12AAAJ&hl=en"), "abcDEF12AAAJ")
        self.assertEqual(scholar.user_id("https://scholar.google.co.uk/citations?hl=no&user=abc-1"), "abc-1")
        self.assertIsNone(scholar.user_id("https://scholar.google.com/scholar?q=hinton"))
        self.assertIsNone(scholar.user_id("https://example.com/citations?user=abc"))

    def test_first_check_dates_papers_by_year_in_profile_order(self):
        with patch.object(scholar, "open_url", return_value=FakeResponse(PROFILE)), \
                patch.object(scholar.time, "time", return_value=NOW):
            feed = scholar.fetch("https://scholar.google.no/citations?user=abcDEF12AAAJ")
        self.assertEqual(feed["title"], "A Researcher")
        self.assertIn("view_photo&user=abcDEF12AAAJ", feed["icon"])
        posts = feed["items"]
        self.assertEqual([p["url"] for p in posts], [url("newer"), url("older"), url("old")])
        self.assertEqual(posts[0]["title"], "Newer & better")
        self.assertEqual(posts[0]["description"], "A Researcher, B Other · Journal 3 (1), 20")
        self.assertEqual(posts[2]["description"], "A Researcher")
        self.assertEqual(posts[0]["published"], (datetime(2026, 1, 1).timestamp() + NOW) / 2)
        self.assertEqual(posts[1]["published"], posts[0]["published"] - 1)
        self.assertEqual(posts[2]["published"], datetime(2019, 7, 1).timestamp())
        self.assertTrue(all(p["date_approx"] and p["scholar"] for p in posts))
        self.assertEqual(feed["scholar_checked"], NOW)

    def test_this_years_papers_are_dated_no_later_than_halfway_to_now(self):
        early = datetime(2026, 3, 1).timestamp()
        self.assertEqual(scholar.year_date(2026, early), (datetime(2026, 1, 1).timestamp() + early) / 2)

    def test_later_checks_keep_dates_and_date_new_recent_papers_now(self):
        papers = scholar.papers_on_page(PROFILE, "abcDEF12AAAJ")
        posts = scholar.dated(papers, {url("older"): 123.0, url("old"): 45.0}, NOW)
        self.assertEqual([p["published"] for p in posts], [NOW, 123.0, 45.0])

    def test_captcha_is_an_error(self):
        sorry = FakeResponse("<html>captcha</html>", "https://www.google.com/sorry/index?continue=x")
        with patch.object(scholar, "open_url", return_value=sorry):
            with self.assertRaisesRegex(feeds.FeedError, "CAPTCHA"):
                scholar.fetch("https://scholar.google.com/citations?user=abcDEF12AAAJ")


class ScholarSourceTests(unittest.TestCase):
    address = "https://scholar.google.com/citations?user=abcDEF12AAAJ"

    def test_detected_and_known_without_fetching_anything_else(self):
        self.assertEqual(server.known_source(self.address, {}), "scholar")
        with patch.object(scholar, "fetch", return_value={"items": []}) as fetch:
            self.assertEqual(server.detect_source(self.address)[0], "scholar")
        fetch.assert_called_once_with(self.address)

    def test_checked_at_most_every_interval_unless_forced(self):
        old = {"scholar_checked": time.time() - 60, "items": [{"url": url("old"), "published": 45.0}]}
        with patch.object(scholar, "fetch", return_value={"items": []}) as fetch:
            self.assertEqual(server.fetch_source("scholar", self.address, old, False), ("scholar", None))
            fetch.assert_not_called()
            server.fetch_source("scholar", self.address, old, True)
            fetch.assert_called_once_with(self.address, {url("old"): 45.0})

    def test_scholar_posts_are_never_enriched_from_their_page(self):
        post = {"url": url("old"), "scholar": True, "date_approx": True, "kind": "subscription"}
        self.assertIsNone(server.enrichment_key(post, {}, set(), NOW))


if __name__ == "__main__":
    unittest.main()
