import unittest

import feeds
import ranking
from test_ranking import item


class YoutubeAgeTests(unittest.TestCase):
    def test_playlist_age_contributes_to_score_without_accessibility_label(self):
        now = 2_000_000_000
        for text, days in [('3 years ago', 1095), ('13d ago', 13), ('2mo ago', 60)]:
            with self.subTest(text=text):
                data = {'lockupViewModel': {
                    'contentId': 'video', 'contentType': 'LOCKUP_CONTENT_TYPE_VIDEO',
                    'metadata': {'title': {'content': 'Video'}, 'metadataParts': [
                        {'text': {'content': '100 views'}, 'accessibilityLabel': '100 views'},
                        {'text': {'content': text}},
                    ]},
                }}
                videos, _ = feeds.videos_on_page(data)
                published = feeds.parse_ago(videos[0]['ago'], now)
                self.assertEqual(published, now - days * 86400)
                post = dict(item('video', 'feed', date=published), date_approx=True)
                weights = dict(ranking.DEFAULT_WEIGHTS, age_per_year=-1, new_sub_bonus=0)
                self.assertIn((f'age {ranking.hours_label(days * 24)}', ranking.points(-days / 365)),
                              ranking.score(post, now, weights))

    def test_accessibility_age_takes_precedence(self):
        data = {'lockupViewModel': {
            'contentId': 'video', 'contentType': 'LOCKUP_CONTENT_TYPE_VIDEO',
            'metadata': {'metadataParts': [{'text': {'content': '3y ago'},
                                           'accessibilityLabel': '3 years ago'}]},
        }}
        self.assertEqual(feeds.videos_on_page(data)[0][0]['ago'], '3 years ago')

    def test_missing_age_stays_unknown(self):
        self.assertIsNone(feeds.parse_ago('100 views', 2_000_000_000))
