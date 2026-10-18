"""Subscription baseline, shared scoring paths, and independent ranking oracle."""
import random
import unittest

import ranking
from test_ranking import NOW, item
from test_above_similarity import greedy_reference


class SubscriptionSimilarityTests(unittest.TestCase):
    def pair(self, days=15):
        return item('first', 'feed'), item('second', 'feed', date=NOW - days * 86400)

    def test_default_base_is_half_and_components_fill_the_remainder(self):
        self.assertEqual(ranking.DEFAULT_WEIGHTS['similar_base_percent'], 50)
        for days, expected in [(0, 100), (12, 80), (15, 75), (30, 50), (365, 50)]:
            with self.subTest(days=days):
                self.assertEqual(ranking.similarity(*self.pair(days), ranking.DEFAULT_WEIGHTS)[0], expected)

    def test_zero_restores_component_only_and_full_base_ignores_components(self):
        first, second = self.pair(12)
        for base, expected in [(0, 60), (25, 70), (50, 80), (100, 100)]:
            weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=base)
            self.assertEqual(ranking.similarity(first, second, weights)[0], expected)

    def test_base_does_not_match_other_subscriptions_or_independent_links(self):
        first, second = self.pair()
        for sub in ['other', None]:
            second['sub'] = sub
            self.assertEqual(ranking.similarity(first, second, ranking.DEFAULT_WEIGHTS)[0], 0)
        first['sub'] = None
        self.assertEqual(ranking.similarity(first, first, ranking.DEFAULT_WEIGHTS)[0], 100)

    def test_base_alone_works_without_available_components(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_publish_weight=0,
                       similar_length_weight=0, similar_title_weight=0)
        first, second = self.pair(365)
        first['last_viewed'] = NOW - 12 * 3600
        ranking.similar_to_recent([first, second], NOW, weights)
        self.assertEqual(second['similar']['percent'], 50)
        self.assertEqual(second['similar']['strength'], 25)
        self.assertEqual(first['similar']['percent'], 100)
        disabled = dict(weights, similar_base_percent=0)
        ranking.similar_to_recent([first, second], NOW, disabled)
        self.assertIsNone(first['similar'])
        self.assertIsNone(second['similar'])

    def test_above_penalty_counts_base_beyond_publish_window(self):
        first, second = self.pair(365)
        ranked = ranking.rank('score', [first, second], NOW)
        self.assertEqual([p['score'] for p in ranked], [0, -7.5])
        self.assertIn('50% total', ranked[1]['reasons'][-1]['label'])
        explained = ranking.explain_similarities(ranked, ranked, ranking.DEFAULT_WEIGHTS)
        match = explained[1]['above_matches'][0]
        self.assertEqual(match['base_percent'], 50)
        self.assertEqual(match['percent'], 50)
        self.assertEqual(match['components'][0]['percent'], 0)

    def test_full_base_does_not_highlight_zero_contribution_title_words(self):
        first, second = self.pair()
        first['title'] = second['title'] = 'CPU design'
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=100, similar_title_weight=1)
        match = ranking.similarity_breakdown(first, second, weights)
        self.assertEqual(match['percent'], 100)
        self.assertTrue(all(not p['shared'] for p in match['title_parts']))

    def test_invalid_percentages_are_rejected(self):
        for value in [-1, 100.01, True, '50', float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ranking.check_weights(dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=value))
        for value in [0, 12.5, 50, 100]:
            self.assertEqual(ranking.check_weights(dict(ranking.DEFAULT_WEIGHTS,
                             similar_base_percent=value))['similar_base_percent'], value)

    def test_random_rankings_match_independent_rational_reference(self):
        for seed in range(20):
            rng = random.Random(seed)
            posts = [dict(item(str(i), rng.choice(['a', 'b', None]),
                               date=NOW - rng.randrange(365) * 86400,
                               age=rng.choice([None, 0, 12, 23, 30])),
                          title=rng.choice(['CPU design', 'CPU 12', 'Bread 12', 'the']),
                          estimated_minutes=rng.choice([None, 5, 10, 20])) for i in range(14)]
            weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=rng.choice([0, 12.5, 50, 100]),
                           similar_publish_weight=rng.choice([0, 1, 2]),
                           similar_length_weight=rng.choice([0, 1, 2]),
                           similar_title_weight=rng.choice([0, 1, 2]))
            with self.subTest(seed=seed):
                self.assertEqual(ranking.rank('score', posts, NOW, weights), greedy_reference(posts, weights))


if __name__ == '__main__':
    unittest.main()
