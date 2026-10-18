import random
import unittest

import ranking
from test_ranking import NOW, item


class ScoreExplanationTests(unittest.TestCase):
    def test_component_inputs_match_weighted_similarity_and_omit_unknowns(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_publish_weight=1,
                       similar_length_weight=1, similar_title_weight=2)
        first = dict(item('first', 'feed'), title='CPU design', estimated_minutes=10)
        second = dict(item('second', 'feed', date=NOW - 15 * 86400),
                      title='CPU design', estimated_minutes=20)
        result = ranking.similarity_breakdown(first, second, weights)
        self.assertEqual(result['percent'], 75)
        self.assertEqual([c['percent'] for c in result['components']], [50, 50, 100])
        self.assertEqual([c['weight'] for c in result['components']], [1, 1, 2])
        self.assertEqual(result['components'][2]['shared_words'], ['cpu', 'design'])
        second['estimated_minutes'] = None
        result = ranking.similarity_breakdown(first, second, weights)
        self.assertIsNone(result['components'][1]['percent'])
        self.assertEqual(result['percent'], 83)

    def test_above_matches_sum_to_penalty_and_recent_match_identifies_source(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_length_weight=1)
        posts = [dict(item(str(i), 'feed', age=1 if i == 0 else None),
                      estimated_minutes=10) for i in range(5)]
        posts.append(item('unrelated', 'other'))
        ranked = ranking.rank('score', posts, NOW, weights, lazy=True)
        visible, _ = ranked.read(6)
        explained = ranking.explain_similarities(visible, ranked.source, weights)
        for card in explained:
            if card.get('similar'):
                self.assertEqual(card['recent_match']['url'], '0')
            for reason in card['reasons']:
                if reason['label'].startswith('similarity to '):
                    total = sum(m['percent'] for m in card['above_matches'])
                    self.assertEqual(reason['points'], -round(weights['above_similarity_penalty'] * total) / 100)
                    self.assertTrue(all(m['url'] != 'unrelated' for m in card['above_matches']))
        self.assertTrue(any(len(c.get('above_matches', [])) >= 3 for c in explained))
        self.assertTrue(all('above_matches' not in c for c in visible))

    def test_highlights_use_ranking_normalization_and_preserve_original_text(self):
        pairs = [('CAFÉ', 'cafe\u0301'), ('STRASSE', 'Straße'), ('CPU', 'ＣＰＵ'),
                 ('Ꭰ', 'ꭰ'), ('ὰι', 'ᾲ'), ('CPU design', 'CPU_design'),
                 ('CPU', 'CPU, CPU!'), ('CPU', '<script>CPU</script>'),
                 ('CPU', '🧑‍💻 CPU'), ('서울', '서울')]
        for first, second in pairs:
            with self.subTest(title=second):
                shared = ranking.title_tokens(first) & ranking.title_tokens(second)
                parts = ranking.highlighted_title_parts(second, shared)
                self.assertEqual(''.join(p['text'] for p in parts), second)
                self.assertTrue(any(p['shared'] for p in parts))
                for part in parts:
                    if part['shared']:
                        self.assertTrue(ranking.title_tokens(part['text']) & shared)
        parts = ranking.highlighted_title_parts('CPU, CPU! GPUs', {'cpu'})
        self.assertEqual([p['text'] for p in parts if p['shared']], ['CPU', 'CPU'])

    def test_random_unicode_spans_preserve_text_and_identify_ranking_tokens(self):
        rng = random.Random(19)
        chars = list('aéİß_ ') + ['\u0301', '\u0327', '\u0345', 'ᾲ', 'ﬃ', 'ᄉ', 'ᅥ', 'ᆯ', 'Ꭰ', 'ꭰ', '😀']
        for _ in range(500):
            title = ''.join(rng.choices(chars, k=rng.randrange(1, 25)))
            shared = ranking.title_tokens(title)
            parts = ranking.highlighted_title_parts(title, shared)
            self.assertEqual(''.join(p['text'] for p in parts), title)
            for part in parts:
                if part['shared']:
                    self.assertTrue(ranking.title_tokens(part['text']) & shared)

    def test_paging_and_disabled_title_component_preserve_explanations(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_title_weight=0)
        posts = [item(str(i), 'feed', date=NOW - i * 86400) for i in range(12)]
        ranked = ranking.rank('score', posts, NOW, weights, lazy=True)
        first, _ = ranked.read(4)
        explained_first = ranking.explain_similarities(first, ranked.source, weights)
        full, _ = ranked.read(12)
        explained_full = ranking.explain_similarities(full, ranked.source, weights)
        self.assertEqual(explained_first, explained_full[:4])
        for card in explained_full:
            for match in card.get('above_matches', []):
                self.assertNotIn('Title', [c['label'] for c in match['components']])
                self.assertTrue(all(not p['shared'] for p in match['title_parts']))

    def test_zero_components_still_count_in_normalization(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_base_percent=0, similar_length_weight=1,
                       similar_title_weight=1, similar_publish_weight=0)
        first = dict(item('first', 'feed'), title='CPU', estimated_minutes=10)
        second = dict(item('second', 'feed'), title='Baking', estimated_minutes=10)
        result = ranking.similarity_breakdown(first, second, weights)
        self.assertEqual(result['percent'], 50)
        self.assertEqual(result['components'][1]['percent'], 0)
        self.assertTrue(all(not p['shared'] for p in result['title_parts']))

    def test_self_match_overrides_component_average(self):
        post = item('self')
        result = ranking.similarity_breakdown(post, post, ranking.DEFAULT_WEIGHTS)
        self.assertTrue(result['self'])
        self.assertEqual(result['percent'], 100)


if __name__ == '__main__':
    unittest.main()
