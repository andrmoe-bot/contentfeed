import copy
import random
import unittest
from fractions import Fraction
from unittest.mock import patch

import ranking


NOW = 2_000_000


def item(url, sub=None, age=None, date=NOW):
    return {"url": url, "title": url, "sub": sub, "date": date,
            "last_viewed": None if age is None else NOW - age * 3600,
            "tags": [], "kind": "subscription" if sub else "link",
            "feed": sub, "position": -1, "color": "white"}


def brute_force(items, weights):
    """The original all-pairs algorithm, with tag similarity removed."""
    recent = [it for it in items if it["last_viewed"] is not None
              and ranking.hours_since(NOW, it["last_viewed"]) < weights["similar_hours"]]
    for it in items:
        best = None
        if weights["similar_penalty"] and (weights["similar_base_percent"] or weights["similar_publish_weight"] or weights["similar_length_weight"] or weights["similar_title_weight"]):
            for other in recent:
                percent, why = ranking.similarity(it, other, weights)
                hours = ranking.hours_since(NOW, other["last_viewed"])
                strength = percent * (1 - hours / weights["similar_hours"])
                if strength and (not best or strength > best["strength"]
                                 or (strength == best["strength"] and why == "itself")):
                    best = {"title": other["title"], "url": other["url"], "hours": hours, "percent": percent,
                            "strength": strength, "why": why, "skipped": other.get("skipped", False)}
        it["similar"] = best


class AgeTests(unittest.TestCase):
    def test_default_is_disabled(self):
        self.assertEqual(ranking.DEFAULT_WEIGHTS['age_per_day'], 0)
        ranked = ranking.rank('score', [item('old', date=NOW - 10 * 86400)], NOW)
        self.assertEqual(ranked[0]['score'], 0)
        self.assertEqual(ranked[0]['reasons'], [])

    def test_signed_age_changes_order_and_explains_score(self):
        for sub in [None, 'feed']:
            for weight, first in [(2, 'old'), (-2, 'new')]:
                with self.subTest(sub=sub, weight=weight):
                    weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, age_per_day=weight,
                                   above_similarity_penalty=0)
                    ranking.check_weights(weights)
                    ranked = ranking.rank('score', [item('old', sub, date=NOW - 36 * 3600),
                                                    item('new', sub)], NOW, weights)
                    self.assertEqual(ranked[0]['url'], first)
                    old = next(it for it in ranked if it['url'] == 'old')
                    self.assertEqual(old['score'], 1.5 * weight)
                    self.assertEqual(old['reasons'], [{'label': 'age 1 day 12 hours',
                                                       'points': 1.5 * weight}])

    def test_whole_hours_and_future_dates(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, age_per_day=-1)
        for seconds, expected in [(-86400, 0), (3599, 0), (3600, -0.04),
                                  (7199, -0.04), (86400, -1)]:
            with self.subTest(seconds=seconds):
                ranked = ranking.rank('score', [item('post', date=NOW - seconds)], NOW, weights)
                self.assertEqual(ranked[0]['score'], expected)


class SimilarityTests(unittest.TestCase):
    def test_index_matches_brute_force(self):
        rng = random.Random(42)
        items = [item(str(i), rng.choice([None, "a", "b", "c"]),
                      rng.choice([None, -1, 0, 1, 12, 23, 24, 48]),
                      NOW - rng.randrange(100) * 86400) for i in range(200)]
        for it in items:
            it["skipped"] = rng.choice([False, True])
            it["estimated_minutes"] = rng.choice([None, 0.5, 10, 20, 120])
        cases = [(24, 30, 40, 1, 0), (24, 30, 40, 1, 1), (24, 30, 40, 0, 1),
                 (24, 30, 40, 0, 0), (1.5, 0.5, 40, 2, 3), (0, 30, 40, 1, 1),
                 (24, 0, 40, 1, 1), (24, 30, 0, 1, 1)]
        for hours, days, penalty, publish, length in cases:
            with self.subTest(hours=hours, days=days, penalty=penalty, publish=publish, length=length):
                w = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_hours=hours,
                         similar_days=days, similar_penalty=penalty,
                         similar_publish_weight=publish, similar_length_weight=length)
                expected, actual = copy.deepcopy(items), copy.deepcopy(items)
                brute_force(expected, w)
                ranking.similar_to_recent(actual, NOW, w)
                self.assertEqual(actual, expected)

    def test_linear_decay_and_cutoff(self):
        w = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, seen_bonus=0)
        for age, expected in [(0, -40), (6, -30), (12, -20), (23, -1.67), (24, 0), (25, 0)]:
            with self.subTest(age=age):
                it = item("self", age=age)
                ranking.similar_to_recent([it], NOW, w)
                self.assertEqual(sum(p for _, p in ranking.score(it, NOW, w)), expected)

    def test_recent_weaker_similarity_can_win(self):
        candidate = item("candidate", "a")
        old = item("old", "a", age=23)
        fresh = item("fresh", "a", age=0, date=NOW - 15 * 86400)
        ranking.similar_to_recent([candidate, old, fresh], NOW, ranking.DEFAULT_WEIGHTS)
        self.assertEqual(candidate["similar"]["title"], "fresh")
        self.assertEqual(candidate["similar"]["strength"], 75)

    def test_self_wins_ties(self):
        first, second = item("first", "a", 0), item("second", "a", 0)
        ranking.similar_to_recent([first, second], NOW, ranking.DEFAULT_WEIGHTS)
        self.assertEqual(first["similar"]["why"], "itself")
        self.assertEqual(second["similar"]["why"], "itself")

    def test_shared_tags_do_not_match(self):
        first, second = item("first", "a"), item("second", "b", 0)
        first["tags"] = second["tags"] = ["science"]
        ranking.similar_to_recent([first, second], NOW, ranking.DEFAULT_WEIGHTS)
        self.assertIsNone(first["similar"])

    def test_only_same_subscription_is_compared_at_scale(self):
        items = [item(str(i), str(i // 200)) for i in range(12000)]
        for i in range(100):
            items[i * 113]["last_viewed"] = NOW
        original = ranking.SimilarityMetric.percent
        calls = 0
        def counted(metric, *args):
            nonlocal calls
            calls += 1
            return original(metric, *args)
        with patch.object(ranking.SimilarityMetric, "percent", counted):
            ranking.similar_to_recent(items, NOW, ranking.DEFAULT_WEIGHTS)
        self.assertEqual(calls, 19_900)  # 20,000 same-sub candidates, including 100 self matches


class NormalizedSimilarityTests(unittest.TestCase):
    def weights(self, publish=1, length=1, **kwargs):
        return dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_publish_weight=publish,
                    similar_length_weight=length, **kwargs)

    def pair(self, days=15, first=10, second=20):
        return (dict(item("a", "feed"), estimated_minutes=first),
                dict(item("b", "feed", date=NOW - days * 86400), estimated_minutes=second))

    def test_weighted_addition_is_normalized(self):
        # Publish-time similarity is 50%, duration similarity is 25%.
        first, second = self.pair(first=10, second=40)
        for publish, length, expected in [(1, 1, 38), (3, 1, 44), (1, 3, 31),
                                           (1, 0, 50), (0, 1, 25), (0.5, 1.5, 31)]:
            with self.subTest(publish=publish, length=length):
                self.assertEqual(ranking.similarity(first, second, self.weights(publish, length))[0], expected)
        # The agreed 15-day, 10/20-minute example is exactly 50%.
        self.assertEqual(ranking.similarity(*self.pair(), self.weights())[0], 50)

    def test_self_is_always_100_percent(self):
        for publish, length in [(1, 1), (5, 2), (0, 1), (1, 0), (0, 0)]:
            for minutes in [None, 0, -1, float('nan'), 10, 300]:
                with self.subTest(publish=publish, length=length, minutes=minutes):
                    it = dict(item("self"), estimated_minutes=minutes)
                    self.assertEqual(ranking.similarity(it, it, self.weights(publish, length)), (100, "itself"))

    def test_identical_components_are_100_for_any_nonzero_weights(self):
        first, second = self.pair(days=0, first=10, second=10)
        for publish, length in [(1, 1), (3, 7), (0, 1), (1, 0), (1e-8, 1e7)]:
            self.assertEqual(ranking.similarity(first, second, self.weights(publish, length))[0], 100)

    def test_unknown_or_invalid_lengths_omit_the_length_weight(self):
        invalid = [None, 0, -10, True, False, '10', float('nan'), float('inf'), -float('inf')]
        for value in invalid:
            for first, second in [(value, 10), (10, value), (value, value)]:
                with self.subTest(first=first, second=second):
                    a, b = self.pair(first=first, second=second)
                    percent, why = ranking.similarity(a, b, self.weights(1, 9))
                    self.assertEqual(percent, 50)
                    self.assertIn('length unknown', why)
                    self.assertEqual(ranking.similarity(a, b, self.weights(0, 1))[0], 0)

    def test_publish_window_boundaries_and_length_beyond_window(self):
        for days, publish_similarity in [(0, 100), (15, 50), (30, 0), (60, 0)]:
            first, second = self.pair(days=days, first=10, second=10)
            self.assertEqual(ranking.similarity(first, second, self.weights(1, 0))[0], publish_similarity)
            self.assertEqual(ranking.similarity(first, second, self.weights())[0],
                             round((publish_similarity + 100) / 2))
        # Zero-day window contributes zero for distinct posts; length still works.
        self.assertEqual(ranking.similarity(*self.pair(), self.weights(similar_days=0))[0], 25)

    def test_length_only_uses_duration_ratio(self):
        for first, second, expected in [(10, 10, 100), (10, 20, 50), (10, 60, 17),
                                        (0.25, 0.5, 50), (1e-9, 1e9, 0)]:
            self.assertEqual(ranking.similarity(*self.pair(first=first, second=second),
                                               self.weights(0, 1))[0], expected)

    def test_same_length_does_not_match_unrelated_subscriptions_or_links(self):
        first, second = self.pair(days=0, first=10, second=10)
        for sub_a, sub_b in [('a', 'b'), (None, None), ('a', None)]:
            first['sub'], second['sub'] = sub_a, sub_b
            self.assertEqual(ranking.similarity(first, second, self.weights())[0], 0)

    def test_both_zero_disables_penalty_including_self(self):
        it = dict(item('opened', 'feed', age=0), estimated_minutes=10, similar={'stale': True})
        weights = self.weights(0, 0, seen_bonus=0)
        with patch.object(ranking, 'similarity', wraps=ranking.similarity) as compare:
            ranked = ranking.rank('score', [it], NOW, weights)
        compare.assert_not_called()
        self.assertEqual(ranked[0]['score'], 0)
        self.assertIsNone(ranked[0]['similar'])

    def test_random_pairs_are_symmetric_bounded_and_scale_invariant(self):
        rng = random.Random(567)
        for _ in range(1000):
            days = rng.choice([0, 1, 15, 30, 60])
            first, second = rng.randint(1, 1000), rng.randint(1, 1000)
            publish, length = rng.choice([(1, 0), (0, 1), (1, 1), (2, 3)])
            a, b = self.pair(days, first, second)
            actual = ranking.similarity(a, b, self.weights(publish, length))[0]
            expected = round(100 * (publish * max(0, 1 - Fraction(days, 30))
                                   + length * Fraction(min(first, second), max(first, second))) / (publish + length))
            self.assertEqual(actual, expected)
            self.assertEqual(actual, ranking.similarity(b, a, self.weights(publish, length))[0])
            self.assertEqual(actual, ranking.similarity(a, b, self.weights(10 * publish, 10 * length))[0])
            self.assertTrue(0 <= actual <= 100)

    def test_default_weights_blend_base_and_publish_time(self):
        self.assertEqual(ranking.DEFAULT_WEIGHTS['similar_length_weight'], 0)
        for days, expected in [(0, 100), (7, 88), (14, 77), (30, 50)]:
            self.assertEqual(ranking.similarity(*self.pair(days, 1, 1000), ranking.DEFAULT_WEIGHTS)[0], expected)

    def test_component_weights_are_validated(self):
        for key in ['similar_publish_weight', 'similar_length_weight']:
            for invalid in [-1, True, '1', float('nan'), float('inf'), ranking.LIMIT + 1]:
                with self.subTest(key=key, value=invalid), self.assertRaises(ValueError):
                    ranking.check_weights(dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, **{key: invalid}))
            for valid in [0, 0.5, ranking.LIMIT]:
                self.assertEqual(ranking.check_weights(dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, **{key: valid}))[key], valid)

    def test_recency_decay_is_applied_after_combining_components(self):
        first, second = self.pair(days=15)
        second['last_viewed'] = NOW - 12 * 3600
        weights = self.weights(seen_bonus=0, above_similarity_penalty=0)
        ranked = ranking.rank('score', [first, second], NOW, weights)
        candidate = next(it for it in ranked if it['url'] == first['url'])
        self.assertEqual(candidate['similar']['percent'], 50)
        self.assertEqual(candidate['similar']['strength'], 25)
        self.assertEqual(candidate['score'], -10)
        reason = candidate['reasons'][0]['label']
        self.assertIn('publish time 50%', reason)
        self.assertIn('length 50%', reason)
        self.assertIn('normalized', reason)
        self.assertIn('50% penalty remaining', reason)

    def test_strongest_combined_match_wins_after_recency_decay(self):
        candidate = dict(item('candidate', 'feed'), estimated_minutes=10)
        near = dict(item('near', 'feed', age=0), estimated_minutes=100)
        far = dict(item('far', 'feed', age=6, date=NOW - 15 * 86400), estimated_minutes=10)
        ranking.similar_to_recent([candidate, near, far], NOW, self.weights())
        self.assertEqual(candidate['similar']['title'], 'far')
        self.assertEqual(candidate['similar']['percent'], 75)
        self.assertEqual(candidate['similar']['strength'], 56.25)

    def test_length_similarity_changes_feed_order(self):
        anchor = dict(item('anchor', 'feed', age=0), estimated_minutes=10)
        same = dict(item('same', 'feed', date=NOW - 30 * 86400), estimated_minutes=10)
        different = dict(item('different', 'feed', date=NOW - 30 * 86400), estimated_minutes=100)
        ranked = ranking.rank('score', [anchor, same, different], NOW,
                              self.weights(seen_bonus=0, above_similarity_penalty=0))
        self.assertEqual([(it['url'], it['score']) for it in ranked],
                         [('different', -2), ('same', -20), ('anchor', -40)])


if __name__ == "__main__":
    unittest.main()
