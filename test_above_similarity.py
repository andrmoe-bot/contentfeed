"""Exact greedy-order oracle and scale coverage for similarity to posts above."""

import copy
from fractions import Fraction
import math
import random
import re
import unicodedata
import unittest
from unittest.mock import patch

import ranking
from test_ranking import item, NOW


def reference_similarity(first, second, weights):
    """Independent normalized calculation using exact rational arithmetic."""
    if first['url'] == second['url']:
        return 100
    if not first.get('sub') or first.get('sub') != second.get('sub'):
        return 0
    publish = Fraction(str(weights['similar_publish_weight']))
    length = Fraction(str(weights['similar_length_weight']))
    days = Fraction(str(weights['similar_days']))
    date_match = max(0, 1 - Fraction(str(abs(first['date'] - second['date']))) / (days * 86400)) if days else 0
    total, normalizer = publish * date_match, publish
    a, b = first.get('estimated_minutes'), second.get('estimated_minutes')
    if all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) and n > 0 for n in (a, b)):
        total += length * Fraction(str(min(a, b))) / Fraction(str(max(a, b)))
        normalizer += length
    title_weight = Fraction(str(weights['similar_title_weight']))
    def words(title):
        if not isinstance(title, str):
            return set()
        return set(re.findall(r"[^\W_]+", unicodedata.normalize('NFKC', title).casefold())) - ranking.TITLE_STOP_WORDS
    first_words, second_words = words(first.get('title')), words(second.get('title'))
    if first_words and second_words:
        total += title_weight * Fraction(2 * len(first_words & second_words), len(first_words) + len(second_words))
        normalizer += title_weight
    base = Fraction(str(weights['similar_base_percent']))
    return round(base + (100 - base) * total / normalizer) if normalizer else round(base)


def greedy_reference(items, weights):
    """Rescore every remaining item against every placed item, then pick the best."""
    base = ranking.rank('score', items, NOW, dict(weights, above_similarity_penalty=0))
    positions = {it['url']: i for i, it in enumerate(items)}
    remaining, out = copy.deepcopy(base), []
    amount = weights['above_similarity_penalty']
    if not (weights['similar_base_percent'] or weights['similar_publish_weight'] or weights['similar_length_weight'] or weights['similar_title_weight']):
        amount = 0
    while remaining:
        candidates = []
        for it in remaining:
            similarities = [reference_similarity(it, other, weights) for other in out] if amount else []
            total = sum(similarities)
            adjustment = round(Fraction(str(amount)) * total) / 100
            score = ranking.points(it['score'] + adjustment)
            key = (score, it['last_viewed'] is None, it['date'], it['position'], -positions[it['url']])
            candidates.append((key, it, total, sum(percent > 0 for percent in similarities), adjustment))
        _, chosen, total, matches, adjustment = max(candidates, key=lambda entry: entry[0])
        remaining.remove(chosen)
        if total and amount:
            chosen['reasons'].append({'label': f"similarity to {matches} post{'s' if matches != 1 else ''} above, {total}% total",
                                      'points': adjustment})
            chosen['score'] = ranking.points(chosen['score'] + adjustment)
        out.append(chosen)
    return out


class AboveSimilarityTests(unittest.TestCase):
    def weights(self, **kwargs):
        return dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, **kwargs)

    def test_partial_matches_add_up_instead_of_fixed_repeat_counts(self):
        posts = [item('first', 'feed'), item('second', 'feed', date=NOW - 15 * 86400),
                 item('third', 'feed', date=NOW - 30 * 86400)]
        result = ranking.rank('score', posts, NOW, self.weights())
        # The 30-day-old item is unrelated to the top post and overtakes the 15-day-old one.
        self.assertEqual([(it['url'], it['score']) for it in result],
                         [('first', 0), ('third', 0), ('second', -15)])
        self.assertEqual(result[-1]['reasons'],
                         [{'label': 'similarity to 2 posts above, 100% total', 'points': -15}])

    def test_identical_posts_accumulate_one_full_penalty_each(self):
        posts = [dict(item(str(i), 'feed'), estimated_minutes=10) for i in range(4)]
        result = ranking.rank('score', posts, NOW, self.weights(similar_length_weight=1))
        self.assertEqual([it['score'] for it in result], [0, -15, -30, -45])
        self.assertEqual([it['url'] for it in result], ['0', '1', '2', '3'])

    def test_group_leader_can_be_overtaken_after_placement(self):
        first = dict(item('first', 'a'), color='green')
        second = item('second', 'a', date=NOW - 86400)
        third = item('third', 'a', date=NOW - 60 * 86400)
        other = item('other', 'b', date=NOW - 90 * 86400)
        posts = [first, second, third, other]
        result = ranking.rank('score', posts, NOW, self.weights(green_bonus_points=30))
        self.assertEqual([it['url'] for it in result], ['first', 'third', 'other', 'second'])
        self.assertEqual(result, greedy_reference(posts, self.weights(green_bonus_points=30)))

    def test_length_similarity_can_match_outside_publish_window(self):
        posts = [dict(item(str(i), 'feed', date=NOW - i * 60 * 86400), estimated_minutes=n)
                 for i, n in enumerate([10, 10, 100])]
        result = ranking.rank('score', posts, NOW, self.weights(similar_publish_weight=0, similar_length_weight=1))
        self.assertEqual([(it['url'], it['score']) for it in result], [('0', 0), ('2', -1.5), ('1', -16.5)])

    def test_missing_lengths_fall_back_to_publish_time(self):
        posts = [dict(item('known', 'feed'), estimated_minutes=20),
                 item('unknown', 'feed', date=NOW - 15 * 86400)]
        result = ranking.rank('score', posts, NOW, self.weights(similar_length_weight=20))
        self.assertEqual(result[-1]['score'], -7.5)
        self.assertEqual(result, greedy_reference(posts, self.weights(similar_length_weight=20)))

    def test_different_subscriptions_and_independent_links_do_not_match(self):
        posts = [dict(item('a', 'a'), estimated_minutes=10), dict(item('b', 'b'), estimated_minutes=10),
                 dict(item('link-a'), estimated_minutes=10), dict(item('link-b'), estimated_minutes=10)]
        result = ranking.rank('score', posts, NOW, self.weights(similar_length_weight=1))
        self.assertTrue(all(it['score'] == 0 and not it['reasons'] for it in result))

    def test_above_penalty_has_no_viewing_time_fade_or_dependency(self):
        posts = [item('first', 'feed', age=1000), item('second', 'feed')]
        weights = self.weights(seen_bonus=0, similar_penalty=0, similar_hours=0)
        result = ranking.rank('score', posts, NOW, weights)
        self.assertEqual([it['url'] for it in result], ['second', 'first'])
        self.assertEqual(result[-1]['score'], -15)

    def test_rounding_uses_total_similarity_not_per_post_rounding(self):
        posts = [dict(item(str(i), 'feed'), estimated_minutes=10) for i in range(5)]
        weights = self.weights(above_similarity_penalty=-0.004)
        result = ranking.rank('score', posts, NOW, weights)
        self.assertEqual([it['score'] for it in result], [0, 0, -0.01, -0.01, -0.02])
        self.assertEqual(result, greedy_reference(posts, weights))
        for it in result:
            self.assertEqual(it['score'], ranking.points(sum(reason['points'] for reason in it['reasons'])))

    def test_zero_amount_or_zero_components_skip_spreading(self):
        posts = [item('a', 'feed'), item('b', 'feed')]
        for weights in [self.weights(above_similarity_penalty=0),
                        self.weights(similar_publish_weight=0, similar_length_weight=0)]:
            with patch.object(ranking, 'spread_by_similarity', wraps=ranking.spread_by_similarity) as spread:
                result = ranking.rank('score', posts, NOW, weights)
            spread.assert_not_called()
            self.assertTrue(all(it['score'] == 0 for it in result))

    def test_chronological_ranker_remains_unaffected(self):
        posts = [item('old', 'feed', age=0, date=NOW - 86400), item('new', 'feed')]
        result = ranking.rank('chronological', posts, NOW, self.weights(similar_length_weight=1))
        self.assertEqual([it['url'] for it in result], ['new', 'old'])
        self.assertTrue(all(it['score'] == 0 and not it['reasons'] for it in result))

    def test_empty_single_and_input_not_mutated(self):
        self.assertEqual(ranking.rank('score', [], NOW), [])
        original = [dict(item('a', 'feed'), reasons=[{'old': True}])]
        snapshot = copy.deepcopy(original)
        result = ranking.rank('score', original, NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(original, snapshot)

    def test_random_orders_match_exhaustive_greedy_reference(self):
        for seed in range(80):
            rng = random.Random(seed)
            posts = []
            for i in range(rng.randint(6, 18)):
                it = item(str(i), rng.choice([None, 'a', 'b', 'c']),
                          age=rng.choice([None, 0, 12, 50]), date=NOW - rng.randrange(60) * 86400)
                it.update(estimated_minutes=rng.choice([None, 0, 5, 10, 20, 40]),
                          color=rng.choice(['white', 'green', 'red']), position=rng.randrange(4))
                if rng.random() < 0.2:
                    it['next_after'] = 'previous'
                posts.append(it)
            weights = self.weights(above_similarity_penalty=rng.choice([0, -0.05, -15, -100, 15]),
                                   green_bonus_points=10, length_per_minute=-0.5,
                                   similar_days=rng.choice([0, 0.5, 30]),
                                   similar_publish_weight=rng.choice([0, 1, 2]),
                                   similar_length_weight=rng.choice([0, 1, 3]))
            with self.subTest(seed=seed):
                self.assertEqual(ranking.rank('score', posts, NOW, weights), greedy_reference(posts, weights))

    def test_scale_50_subscriptions_10000_items_avoids_cross_subscription_work(self):
        posts = [dict(item(str(i), str(i % 50), date=NOW - (i // 50) * 2 * 86400),
                      estimated_minutes=10) for i in range(10_000)]
        original = ranking.SimilarityMetric.percent
        calls = 0

        def counted(metric, *args):
            nonlocal calls
            calls += 1
            return original(metric, *args)

        with patch.object(ranking.SimilarityMetric, 'percent', counted):
            result = ranking.rank('score', posts, NOW, self.weights())
        self.assertEqual(len(result), 10_000)
        self.assertEqual(len({it['url'] for it in result}), 10_000)
        # At most 15 neighboring dates in either direction, each unordered pair once.
        self.assertEqual(calls, 50 * sum(200 - distance for distance in range(1, 16)))
        self.assertLess(calls, 150_000)  # versus ~50 million cross-subscription comparisons


    def test_progressive_pages_match_full_order_and_do_not_mutate_previous_pages(self):
        from benchmark_ranking import fixture
        posts = fixture(count=200, subscriptions=5)
        for length_weight in [0, 1]:
            weights = self.weights(similar_length_weight=length_weight)
            expected = ranking.rank('score', posts, NOW, weights)
            lazy = ranking.rank('score', posts, NOW, weights, lazy=True)
            first, _ = lazy.read(20)
            snapshot = copy.deepcopy(first)
            self.assertEqual(first, expected[:20])
            self.assertEqual(len(lazy.items), 20)
            for size in [50, 25, 100, 200, 300]:
                page, _ = lazy.read(size)
                self.assertEqual(page, expected[:size])
            self.assertEqual(first, snapshot)

    def test_concurrent_page_extensions_remain_exact(self):
        from concurrent.futures import ThreadPoolExecutor
        from benchmark_ranking import fixture
        posts = fixture(count=200, subscriptions=5)
        expected = ranking.rank('score', posts, NOW)
        lazy = ranking.rank('score', posts, NOW, lazy=True)
        sizes = [20, 150, 50, 100, 200, 30]
        with ThreadPoolExecutor(max_workers=6) as pool:
            pages = list(pool.map(lambda size: lazy.read(size)[0], sizes))
        for size, page in zip(sizes, pages):
            self.assertEqual(page, expected[:size])
        self.assertEqual(len(lazy.items), 200)

    def test_metadata_search_can_extend_past_visible_page_without_losing_order(self):
        posts = [dict(item(str(i)), estimated_minutes=10 if i < 25 else None) for i in range(40)]
        lazy = ranking.rank('score', posts, NOW, lazy=True)
        visible, extra = lazy.read(10, 5, lambda it: it['url'] if not it.get('estimated_minutes') else None)
        self.assertEqual(len(visible), 10)
        self.assertEqual(len(extra), 30)
        self.assertEqual([it['url'] for it in extra[-5:]], ['25', '26', '27', '28', '29'])
        self.assertEqual([it['url'] for it in lazy.read(40)[0]], [str(i) for i in range(40)])

    def test_no_eligible_metadata_does_not_force_full_ranking(self):
        posts = [dict(item(str(i), 'a'), estimated_minutes=10) for i in range(100)]
        lazy = ranking.rank('score', posts, NOW, lazy=True)
        visible, extra = lazy.read(10, 5, lambda it: None)
        self.assertEqual(len(visible), 10)
        self.assertEqual(len(extra), 10)
        self.assertEqual(len(lazy.items), 10)

    def test_visible_page_at_scale_visits_only_selected_groups(self):
        posts = [dict(item(str(i), str(i % 50), date=NOW - (i // 50) * 86400),
                      estimated_minutes=10) for i in range(10_000)]
        original = ranking.SimilarityMetric.percent
        calls = 0

        def counted(metric, *args):
            nonlocal calls
            calls += 1
            return original(metric, *args)

        with patch.object(ranking.SimilarityMetric, 'percent', counted):
            lazy = ranking.rank('score', posts, NOW, self.weights(similar_length_weight=1), lazy=True)
            visible, _ = lazy.read(300)
        self.assertEqual(len(visible), 300)
        self.assertEqual(lazy.total, 10_000)
        self.assertEqual(len(lazy.items), 300)
        self.assertLess(calls, 60_000)  # no eager processing of all ~1 million same-sub pairs
