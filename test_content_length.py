import unittest

from content_length import duration_minutes, page_minutes
from feeds import MetaParser, parse
import ranking
from test_ranking import item, NOW


class ContentLengthTests(unittest.TestCase):
    def test_duration_units(self):
        for value, minutes in [('PT1H30M', 90), ('P1DT30M', 1470), ('1:30', 1.5), (120, 2)]:
            self.assertEqual(duration_minutes(value), minutes)
        for value in [None, '', 'live', float('nan'), float('inf'), -10, True]:
            self.assertIsNone(duration_minutes(value))

    def test_page_estimates(self):
        parser = MetaParser()
        parser.feed('<script>ignored words</script><article>' + 'word ' * 400 + '</article>')
        self.assertEqual(page_minutes(parser), 2)
        self.assertIsNone(page_minutes(parser, video=True))
        parser.feed('<script type="application/ld+json">{"@type":"VideoObject","duration":"PT5M"}</script>')
        self.assertEqual(page_minutes(parser, video=True), 5)

    def test_feed_uses_full_text_before_truncating(self):
        body = ('<rss><channel><item><link>https://example.com/a</link><description>'
                + 'word ' * 600 + '</description></item></channel></rss>')
        self.assertEqual(parse(body.encode(), 'https://example.com')['items'][0]['estimated_minutes'], 3)

    def test_signed_length_changes_order_and_explains_score(self):
        short, long, unknown = item('short'), item('long'), item('unknown')
        short['estimated_minutes'], long['estimated_minutes'] = 2, 10
        for weight, first in [(2, 'long'), (-2, 'unknown')]:
            weights = dict(ranking.DEFAULT_WEIGHTS, length_per_minute=weight)
            ranking.check_weights(weights)
            ranked = ranking.rank('score', [short, long, unknown], NOW, weights)
            self.assertEqual(ranked[0]['url'], first)
            result = next(it for it in ranked if it['url'] == 'long')
            self.assertEqual(result['score'], 10 * weight)
            self.assertEqual(result['reasons'], [{'label': 'estimated 10 minutes', 'points': 10 * weight}])
        self.assertEqual(ranking.DEFAULT_WEIGHTS['length_per_minute'], 0)
        self.assertTrue(all(it['score'] == 0 for it in ranking.rank('score', [short, long, unknown], NOW)))
