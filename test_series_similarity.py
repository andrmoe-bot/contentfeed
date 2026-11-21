"""Posts in the same series start from the series base similarity."""

import copy
import random
import unittest

import ranking
import series
from test_above_similarity import reference_similarity, greedy_reference
from test_ranking import item, brute_force, NOW


def post(url, sub='feed', series_ids=(), **extra):
    return dict(item(url, sub), series_ids=list(series_ids), **extra)


class SeriesSimilarityTests(unittest.TestCase):
    def terms(self, base=50, series_base=75, **extra):
        return dict(ranking.DEFAULT_TERMS, similar_base_percent=base, similar_series_percent=series_base, **extra)

    def test_default_series_base(self):
        self.assertEqual(ranking.DEFAULT_TERMS['similar_series_percent'], 75)

    def test_same_series_uses_series_base(self):
        first = post('a', series_ids=['playlist:x'], date=NOW)
        second = post('b', series_ids=['playlist:y', 'playlist:x'], date=NOW - 15 * 86400)
        other = post('c', series_ids=['playlist:y'], date=NOW - 15 * 86400)
        # 15 of 30 days apart: 50% publish-time similarity fills the remainder
        self.assertEqual(ranking.similarity(first, second, self.terms())[0], 88)
        self.assertEqual(ranking.similarity(first, other, self.terms())[0], 75)
        self.assertIn('same series', ranking.similarity(first, second, self.terms())[1])
        self.assertIn('same subscription', ranking.similarity(first, other, self.terms())[1])

    def test_series_base_never_lowers_the_subscription_base(self):
        first, second = post('a', series_ids=['s']), post('b', series_ids=['s'], date=NOW - 15 * 86400)
        terms = self.terms(base=50, series_base=20)
        self.assertEqual(ranking.similarity(first, second, terms)[0], 75)
        self.assertIn('same subscription', ranking.similarity(first, second, terms)[1])
        match = ranking.similarity_breakdown(first, second, terms)
        self.assertEqual((match['base_percent'], match['same_series']), (50, False))

    def test_breakdown_reports_the_series_base(self):
        match = ranking.similarity_breakdown(post('a', series_ids=['s']), post('b', series_ids=['s']), self.terms())
        self.assertEqual((match['base_percent'], match['same_series']), (75, True))

    def test_other_subscriptions_still_do_not_match(self):
        first, second = post('a', 'one', ['s']), post('b', 'two', ['s'])
        self.assertEqual(ranking.similarity(first, second, self.terms()), (0, 'different subscriptions'))

    def test_series_base_alone_enables_similarity(self):
        terms = self.terms(base=0, series_base=100, similar_publish_weight=0, above_similarity_penalty=-10)
        posts = [post('a', series_ids=['s']), post('b', series_ids=['s']), post('c')]
        result = {it['url']: it for it in ranking.rank('score', posts, NOW, terms)}
        self.assertEqual(result['b']['score'], -10)
        self.assertEqual(result['c']['score'], 0)

    def test_randomized_series_match_independent_reference(self):
        rng = random.Random(41)
        for seed in range(40):
            posts = [post(str(i), rng.choice(['a', 'b']), rng.sample(['s', 't', 'u'], rng.randrange(3)),
                          date=NOW - rng.randrange(60) * 86400, estimated_minutes=rng.choice([None, 5, 10]),
                          title=rng.choice(['Part 1', 'Part 2', 'Bread', None]))
                     for i in range(10)]
            for p in posts:
                if rng.random() < 0.3:
                    p['last_viewed'] = NOW - rng.randrange(24) * 3600
            terms = self.terms(base=rng.choice([0, 25, 50]), series_base=rng.choice([0, 40, 75, 100]),
                               similar_publish_weight=rng.choice([0, 1]), similar_length_weight=rng.choice([0, 1]))
            with self.subTest(seed=seed):
                for first in posts:
                    for second in posts:
                        self.assertEqual(ranking.similarity(first, second, terms)[0],
                                         reference_similarity(first, second, terms))
                self.assertEqual(ranking.rank('score', posts, NOW, terms), greedy_reference(posts, terms))
                expected, actual = copy.deepcopy(posts), copy.deepcopy(posts)
                brute_force(expected, terms)
                ranking.similar_to_recent(actual, NOW, terms)
                self.assertEqual(actual, expected)


class MembershipTests(unittest.TestCase):
    def test_title_series_and_loose_posts(self):
        posts = [{'url': u, 'title': t} for u, t in
                 [('e', 'CPU part 3'), ('d', 'Bread'), ('c', 'CPU part 2'), ('b', 'GPU 1 of 2'),
                  ('a', 'CPU part 1')]]
        found = series.memberships({'items': posts})
        self.assertEqual(found, {'a': ['titles:a'], 'c': ['titles:a'], 'e': ['titles:a']})
        self.assertEqual({s['id'] for s in series.of_post({'items': posts}, 'c')}, {'titles:a'})

    def test_playlists_and_episodes(self):
        sub = {'items': [{'url': 'v1', 'youtube': 'y1'}, {'url': 'v2', 'youtube': 'y2'}, {'url': 'v3', 'youtube': 'y3'}],
               'youtube_playlists': [{'id': 'p', 'videos': ['y1', 'y2']}, {'id': 'q', 'videos': ['y2']}]}
        self.assertEqual(series.memberships(sub), {'v1': ['playlist:p'], 'v2': ['playlist:p', 'playlist:q']})
        shows = {'items': [{'url': 'e1', 'series': 7, 'episode': [1, 1]}, {'url': 'e2', 'series': 7, 'episode': [1, 2]},
                           {'url': 'n', 'no_series': True}]}
        self.assertEqual(series.memberships(shows), {'e1': ['series:7'], 'e2': ['series:7']})


if __name__ == '__main__':
    unittest.main()
