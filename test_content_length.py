import unittest

from content_length import duration_minutes, page_minutes, youtube_minutes
from feeds import MetaParser, parse
import ranking
from test_ranking import item, NOW


class ContentLengthTests(unittest.TestCase):
    def test_youtube_player_runtime(self):
        self.assertEqual(youtube_minutes('<script>{"videoDetails":{"lengthSeconds":"750"}}</script>'), 12.5)
        self.assertEqual(youtube_minutes('{"approxDurationMs":"90000"}'), 1.5)
        self.assertIsNone(youtube_minutes('{"isLive":true}'))

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
            self.assertEqual(result['reasons'], [{'label': 'estimated 10 minutes, 10 minutes from target 0', 'points': 10 * weight}])
        self.assertEqual(ranking.DEFAULT_WEIGHTS['length_per_minute'], 0)
        self.assertTrue(all(it['score'] == 0 for it in ranking.rank('score', [short, long, unknown], NOW)))

    def test_weighted_distance_from_target(self):
        posts = [dict(item(str(minutes)), estimated_minutes=minutes) for minutes in (5, 10, 15, 20)]
        for weight in (-2, 2):
            weights = dict(ranking.DEFAULT_WEIGHTS, length_per_minute=weight, length_target_minutes=10)
            ranking.check_weights(weights)
            ranked = ranking.rank('score', posts, NOW, weights)
            scores = {post['url']: post['score'] for post in ranked}
            self.assertEqual(scores, {'5': 5 * weight, '10': 0, '15': 5 * weight, '20': 10 * weight})
            self.assertEqual(ranked[0]['url'], '10' if weight < 0 else '20')
            for invalid in (None, 0, -1, True, float('nan'), float('inf')):
                self.assertEqual(ranking.score(dict(item('unknown'), estimated_minutes=invalid), NOW, weights), [])
        for invalid in (-1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                ranking.check_weights(dict(weights, length_target_minutes=invalid))

    def test_watch_page_continues_after_date_to_find_runtime(self):
        import io
        from unittest.mock import patch
        import feeds
        body = b'<meta itemprop="datePublished" content="2020-01-01">'
        body += b' ' * 256000 + b'{"lengthSeconds":"120"}'
        with patch.object(feeds, 'open_url', return_value=io.BytesIO(body)):
            result = feeds.video_metadata('abc')
        self.assertEqual(result['estimated_minutes'], 2)
        self.assertEqual(result['published'], feeds.parse_date('2020-01-01'))
