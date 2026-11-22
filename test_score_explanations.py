import unittest

import ranking
from test_ranking import NOW, item, TERMS_ON


class ScoreExplanationTests(unittest.TestCase):
    def test_component_inputs_match_weighted_similarity_and_omit_unknowns(self):
        terms = dict(TERMS_ON, similar_base_percent=0, similar_series_percent=0, similar_publish_weight=1,
                       similar_length_weight=3)
        first = dict(item('first', 'feed'), title='CPU design', estimated_minutes=10)
        second = dict(item('second', 'feed', date=NOW - 15 * 86400),
                      title='CPU design', estimated_minutes=20)
        result = ranking.similarity_breakdown(first, second, terms)
        self.assertEqual(result['percent'], 50)
        self.assertEqual([c['percent'] for c in result['components']], [50, 50])
        self.assertEqual([c['weight'] for c in result['components']], [1, 3])
        second['estimated_minutes'] = None
        result = ranking.similarity_breakdown(first, second, terms)
        self.assertIsNone(result['components'][1]['percent'])
        self.assertEqual(result['percent'], 50)

    def test_above_matches_sum_to_penalty_and_recent_match_identifies_source(self):
        terms = dict(TERMS_ON, similar_base_percent=0, similar_series_percent=0, similar_length_weight=1)
        posts = [dict(item(str(i), 'feed', age=1 if i == 0 else None),
                      estimated_minutes=10) for i in range(5)]
        posts.append(item('unrelated', 'other'))
        ranked = ranking.rank('score', posts, NOW, terms, lazy=True)
        visible = ranked.read(6)
        explained = ranking.explain_similarities(visible, ranked.source, terms, NOW)
        for card in explained:
            if card.get('similar'):
                self.assertEqual(card['recent_match']['url'], '0')
                self.assertEqual([m['url'] for m in card['recent_matches']], ['0'])
            for reason in card['reasons']:
                if reason['label'].startswith('similarity to '):
                    total = sum(m['percent'] for m in card['above_matches'])
                    self.assertEqual(reason['points'], round(terms['above_similarity_penalty'] * total) / 100)
                    self.assertTrue(all(m['url'] != 'unrelated' for m in card['above_matches']))
        self.assertTrue(any(len(c.get('above_matches', [])) >= 3 for c in explained))
        self.assertTrue(all('above_matches' not in c for c in visible))

    def test_paging_preserves_explanations(self):
        terms = dict(TERMS_ON, similar_base_percent=0, similar_series_percent=0)
        posts = [item(str(i), 'feed', date=NOW - i * 86400) for i in range(12)]
        ranked = ranking.rank('score', posts, NOW, terms, lazy=True)
        first = ranked.read(4)
        explained_first = ranking.explain_similarities(first, ranked.source, terms, NOW)
        full = ranked.read(12)
        explained_full = ranking.explain_similarities(full, ranked.source, terms, NOW)
        self.assertEqual(explained_first, explained_full[:4])

    def test_zero_components_still_count_in_normalization(self):
        terms = dict(TERMS_ON, similar_base_percent=0, similar_series_percent=0, similar_length_weight=1,
                       similar_publish_weight=1, similar_days=10)
        first = dict(item('first', 'feed'), estimated_minutes=10)
        second = dict(item('second', 'feed', date=NOW - 20 * 86400), estimated_minutes=10)
        result = ranking.similarity_breakdown(first, second, terms)
        self.assertEqual(result['percent'], 50)
        self.assertEqual(result['components'][0]['percent'], 0)

    def test_self_match_overrides_component_average(self):
        post = item('self')
        result = ranking.similarity_breakdown(post, post, ranking.DEFAULT_TERMS)
        self.assertTrue(result['self'])
        self.assertEqual(result['percent'], 100)


if __name__ == '__main__':
    unittest.main()
