"""The Middle author setting: points for papers where the authors you follow are neither first nor last."""
import unittest

import authors
import feeds
import ranking
import scholar
from test_authors import PROFILE

NOW = 2_000_000
TERMS = dict(ranking.DEFAULT_TERMS, middle_author_points=-1000)


def paper(names, *followed, cut=False):
    return {"url": "https://arxiv.org/abs/2401.00001", "title": "A paper", "sub": "s", "date": NOW,
            "last_viewed": None, "tags": [], "kind": "subscription", "feed": "s", "position": -1,
            "authors": names, "authors_cut": cut,
            "via": [{"kind": "author", "name": name, "sub": "s"} for name in followed]}


def middle_points(item, terms=TERMS):
    return sum(p for label, p in ranking.score(item, NOW, terms) if " is author " in label)


FIVE = ["Ada Lovelace", "Yann LeCun", "Léon Bottou", "Yoshua Bengio", "Patrick Haffner"]


class NameTests(unittest.TestCase):
    def test_names_written_differently_match(self):
        for name in ("Yann LeCun", "LeCun, Yann", "Y LeCun", "Y. LeCun", "yann lecun", "LeCun"):
            self.assertEqual(authors.position(FIVE, name), 1, name)
        self.assertEqual(authors.position(FIVE, "Leon Bottou"), 2)  # without the accent
        self.assertEqual(authors.position(["John Smith Jr."], "Smith, John"), 0)

    def test_same_surname_other_initial_is_someone_else(self):
        self.assertIsNone(authors.position(FIVE, "Samy Bengio"))
        self.assertIsNone(authors.position(FIVE, "Geoffrey Hinton"))

    def test_scholar_line_cut_short(self):
        self.assertEqual(authors.scholar_names("A Vaswani, N Shazeer, N Parmar, ..."),
                         (["A Vaswani", "N Shazeer", "N Parmar"], True))
        self.assertEqual(authors.scholar_names("A Researcher, B Other"), (["A Researcher", "B Other"], False))
        self.assertEqual(authors.scholar_names(""), ([], False))


class ScoreTests(unittest.TestCase):
    def test_middle_author_gets_the_points(self):
        reasons = ranking.score(paper(FIVE, "LeCun, Yann"), NOW, TERMS)
        self.assertIn(("LeCun, Yann is author 2 of 5", -1000), reasons)

    def test_first_or_last_author_or_unknown_place_gets_none(self):
        for name in ("Ada Lovelace", "Patrick Haffner", "Geoffrey Hinton"):
            self.assertEqual(middle_points(paper(FIVE, name)), 0, name)
        self.assertEqual(middle_points(paper([], "Yann LeCun")), 0)
        self.assertEqual(middle_points(dict(paper(FIVE), authors=None, via=[])), 0)

    def test_off_by_default_and_needs_enough_authors(self):
        self.assertEqual(middle_points(paper(FIVE, "Yann LeCun"), ranking.DEFAULT_TERMS), 0)
        self.assertEqual(middle_points(paper(FIVE[:3], "Yann LeCun")), 0)  # 3 authors, fewer than 4
        self.assertEqual(middle_points(paper(FIVE[:3], "Yann LeCun"), dict(TERMS, middle_author_min_count=3)), -1000)

    def test_every_author_you_follow_must_be_in_the_middle(self):
        self.assertEqual(middle_points(paper(FIVE, "Yann LeCun", "Ada Lovelace")), 0)
        item = paper(FIVE, "Yann LeCun", "Bengio, Yoshua")
        self.assertIn(("Yann LeCun is author 2 of 5; Bengio, Yoshua is author 4 of 5", -1000),
                      ranking.score(item, NOW, TERMS))

    def test_cut_list_hides_the_last_author(self):
        names = ["A Vaswani", "N Shazeer", "N Parmar"]
        # The last shown author isn't the paper's last, which the list leaves out
        self.assertIn(("Niki Parmar is author 3 of 4+", -1000),
                      ranking.score(paper(names, "Niki Parmar", cut=True), NOW, TERMS))
        self.assertEqual(middle_points(paper(names, "Ashish Vaswani", cut=True)), 0)
        self.assertEqual(middle_points(paper(names, "Llion Jones", cut=True)), 0)  # not shown
        self.assertEqual(middle_points(paper(names, "Niki Parmar", cut=True), dict(TERMS, middle_author_min_count=5)), 0)

    def test_author_count_is_checked(self):
        self.assertEqual(ranking.check_terms(dict(TERMS))["middle_author_min_count"], 4)
        with self.assertRaises(ValueError):
            ranking.check_terms(dict(TERMS, middle_author_min_count=-1))


class SourceTests(unittest.TestCase):
    def test_arxiv_posts_keep_every_author_in_order(self):
        body = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
          <entry><id>http://arxiv.org/abs/2401.00001v1</id><title>A paper</title>
            <author><name>Ada Lovelace</name></author><author><name>Yann LeCun</name>
            <arxiv:affiliation xmlns:arxiv="http://arxiv.org/schemas/atom">NYU</arxiv:affiliation></author>
            <link href="http://arxiv.org/abs/2401.00001v1" rel="alternate"/></entry>
        </feed>"""
        post = feeds.parse(body, "https://export.arxiv.org/api/query")["items"][0]
        self.assertEqual(post["authors"], ["Ada Lovelace", "Yann LeCun"])

    def test_other_feeds_dont_store_authors(self):
        body = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
          <entry><title>Post</title><author><name>Me</name></author><link href="https://blog.example/p"/></entry>
        </feed>"""
        self.assertNotIn("authors", feeds.parse(body, "https://blog.example/feed")["items"][0])

    def test_scholar_posts_keep_the_shown_authors(self):
        posts = scholar.dated(scholar.papers_on_page(PROFILE.replace("A Researcher, B Other", "B Other, A Researcher, C Third, ..."),
                                                     "abcDEF12AAAJ"), {}, NOW)
        self.assertEqual((posts[0]["authors"], posts[0]["authors_cut"]), (["B Other", "A Researcher", "C Third"], True))
        self.assertEqual((posts[1]["authors"], posts[1]["authors_cut"]), (["A Researcher"], False))


if __name__ == "__main__":
    unittest.main()
