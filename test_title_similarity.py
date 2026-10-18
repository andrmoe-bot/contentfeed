"""Weighted title overlap, shared ranking paths, and bounded tokenization work."""

import copy
import random
import unittest

import ranking
from test_ranking import NOW, item, brute_force
from test_above_similarity import reference_similarity, greedy_reference


class TitleSimilarityTests(unittest.TestCase):
    def weights(self, publish=0, length=0, title=1, **extra):
        return dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_publish_weight=publish,
                    similar_length_weight=length, similar_title_weight=title, **extra)

    def pair(self, first='Building a CPU, part 1', second='Building a CPU, part 2', days=15):
        return (dict(item('a', 'feed'), title=first, estimated_minutes=10),
                dict(item('b', 'feed', date=NOW - days * 86400), title=second, estimated_minutes=20))

    def test_agreed_title_overlap_example_is_75_percent(self):
        first, second = self.pair()
        self.assertEqual(ranking.similarity(first, second, self.weights())[0], 75)
        self.assertEqual(ranking.similarity(second, first, self.weights())[0], 75)

    def test_case_punctuation_stopwords_and_duplicate_words(self):
        for first, second in [('The CPU: DESIGN!', 'cpu design'), ('CPU CPU cpu design', 'CPU design'),
                              ('CPU_design', 'CPU design'), ('CPU—design', 'cpu design')]:
            self.assertEqual(ranking.similarity(*self.pair(first, second), self.weights())[0], 100)

    def test_unicode_and_composed_characters(self):
        for first, second in [('CAFÉ', 'cafe\u0301'), ('Straße', 'STRASSE'),
                              ('ＣＰＵ design', 'CPU design'), ('Norsk blåbær', 'NORSK BLÅBÆR')]:
            self.assertEqual(ranking.similarity(*self.pair(first, second), self.weights())[0], 100)

    def test_numbers_are_kept_and_repeated_words_do_not_inflate_similarity(self):
        self.assertEqual(ranking.title_tokens('Episode 12: CPU 6502'), {'episode', '12', 'cpu', '6502'})
        self.assertEqual(ranking.similarity(*self.pair('Episode 1', 'Episode 2'), self.weights())[0], 50)
        self.assertEqual(ranking.similarity(*self.pair('CPU CPU CPU', 'CPU GPU'), self.weights())[0], 67)

    def test_unrelated_titles_are_zero(self):
        self.assertEqual(ranking.similarity(*self.pair('CPU architecture', 'Baking bread'), self.weights())[0], 0)

    def test_three_components_add_and_normalize(self):
        first, second = self.pair()  # publish 50%, length 50%, title 75%
        for publish, length, title, expected in [(1, 1, 1, 58), (1, 1, 2, 62),
                                                (0, 1, 1, 62), (1, 0, 1, 62), (0, 0, 1, 75)]:
            with self.subTest(publish=publish, length=length, title=title):
                weights = self.weights(publish, length, title)
                self.assertEqual(ranking.similarity(first, second, weights)[0], expected)
                self.assertEqual(ranking.similarity(first, second, self.weights(10*publish, 10*length, 10*title))[0], expected)

    def test_unknown_lengths_retain_publish_and_title_weights_only(self):
        first, second = self.pair()
        first.pop('estimated_minutes')
        self.assertEqual(ranking.similarity(first, second, self.weights(1, 100, 1))[0], 62)
        first.pop('title')
        self.assertEqual(ranking.similarity(first, second, self.weights(1, 100, 100))[0], 50)

    def test_empty_or_stopword_only_titles_are_omitted_not_matched(self):
        for missing in [None, '', '!!!', 'the a and', 123]:
            for first_title, second_title in [(missing, 'CPU design'), ('CPU design', missing), (missing, missing)]:
                with self.subTest(first=first_title, second=second_title):
                    first, second = self.pair(first_title, second_title)
                    self.assertEqual(ranking.similarity(first, second, self.weights(1, 1, 100))[0], 50)
                    self.assertEqual(ranking.similarity(first, second, self.weights())[0], 0)

    def test_self_is_100_even_without_any_usable_title(self):
        for title in [None, '', 'the', 'CPU design']:
            first, _ = self.pair(title)
            for weights in [self.weights(), self.weights(1, 2, 3), self.weights(0, 0, 0)]:
                self.assertEqual(ranking.similarity(first, first, weights), (100, 'itself'))

    def test_subscriptions_and_link_boundaries_are_preserved(self):
        first, second = self.pair('CPU design', 'CPU design')
        for a, b in [('one', 'two'), (None, None), ('one', None)]:
            first['sub'], second['sub'] = a, b
            self.assertEqual(ranking.similarity(first, second, self.weights())[0], 0)

    def test_title_only_recent_view_penalty_and_reason(self):
        first, second = self.pair()
        second['last_viewed'] = NOW - 12 * 3600
        weights = self.weights(seen_bonus=0, above_similarity_penalty=0)
        ranked = ranking.rank('score', [first, second], NOW, weights)
        candidate = next(it for it in ranked if it['url'] == first['url'])
        self.assertEqual(candidate['similar']['percent'], 75)
        self.assertEqual(candidate['score'], -15)  # 40 × 75% × half the viewing window
        self.assertIn('title 75% × 1', candidate['reasons'][0]['label'])

    def test_title_only_above_penalty_works_beyond_publish_window(self):
        first, second = self.pair(days=365)
        third = dict(item('c', 'feed', date=NOW - 400*86400), title='Baking bread')
        result = ranking.rank('score', [first, second, third], NOW, self.weights())
        self.assertEqual([(it['url'], it['score']) for it in result], [('a', 0), ('c', 0), ('b', -11.25)])
        self.assertEqual(result, greedy_reference([first, second, third], self.weights()))

    def test_all_zero_components_disable_both_penalties(self):
        first, second = self.pair()
        first['last_viewed'] = NOW
        result = ranking.rank('score', [first, second], NOW, self.weights(0, 0, 0, seen_bonus=0))
        self.assertTrue(all(it['similar'] is None and it['score'] == 0 for it in result))

    def test_default_and_validation(self):
        self.assertEqual(ranking.DEFAULT_WEIGHTS['similar_title_weight'], 0)
        for value in [-1, True, '1', float('nan'), float('inf'), ranking.LIMIT + 1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                ranking.check_weights(self.weights(title=value))
        for value in [0, 0.5, ranking.LIMIT]:
            self.assertEqual(ranking.check_weights(self.weights(title=value))['similar_title_weight'], value)

    def test_randomized_similarity_and_greedy_order_match_independent_reference(self):
        rng = random.Random(237)
        titles = ['Building a CPU part 1', 'Building a CPU part 2', 'CPU design',
                  'Baking bread', 'Baking sourdough bread', 'The GPU version 3', None, 'the and']
        for seed in range(50):
            posts = [dict(item(str(i), rng.choice([None, 'a', 'b']), age=rng.choice([None, 0, 12]),
                               date=NOW - rng.randrange(90)*86400), title=rng.choice(titles),
                          estimated_minutes=rng.choice([None, 0, 5, 10, 20]),
                          color=rng.choice(['white', 'green', 'red'])) for i in range(12)]
            weights = self.weights(rng.choice([0, 1, 3]), rng.choice([0, 1, 2]), rng.choice([0, 0.5, 1, 2]))
            with self.subTest(seed=seed):
                for first, second in zip(posts, posts[1:]):
                    self.assertEqual(ranking.similarity(first, second, weights)[0], reference_similarity(first, second, weights))
                self.assertEqual(ranking.rank('score', posts, NOW, weights), greedy_reference(posts, weights))
                expected, actual = copy.deepcopy(posts), copy.deepcopy(posts)
                brute_force(expected, weights)
                ranking.similar_to_recent(actual, NOW, weights)
                self.assertEqual(actual, expected)

    def test_tokenization_is_cached_and_pagination_remains_exact_at_scale(self):
        from benchmark_ranking import fixture, NOW as BENCH_NOW
        posts = fixture()
        ranking.title_tokens.cache_clear()
        lazy = ranking.rank('score', posts, BENCH_NOW, self.weights(1, 1, 1), lazy=True)
        first, _ = lazy.read(300)
        self.assertEqual(len(first), 300)
        self.assertEqual(lazy.total, 10_000)
        self.assertEqual(len(lazy.items), 300)
        self.assertEqual(ranking.title_tokens.cache_info().misses, 10_000)
        snapshot = copy.deepcopy(first)
        next_page, _ = lazy.read(600)
        self.assertEqual(next_page[:300], snapshot)
        self.assertEqual(ranking.title_tokens.cache_info().misses, 10_000)
