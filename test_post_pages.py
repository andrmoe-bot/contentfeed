import contextlib
import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import ranking
import server


class PostPageTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ('LINKS_FILE', 'FEEDS_FILE', 'ADDED_FILE', 'VIEWED_FILE', 'COLORS_FILE', 'SKIPPED_FILE'):
            stack.enter_context(patch.object(server, name, root / name))
        for name in ('subscriptions', 'cache', 'added', 'viewed', 'skipped', 'colors', 'ranked_cache'):
            stack.enter_context(patch.object(server, name, {}))
        stack.enter_context(patch.object(server, 'pending', set()))
        stack.enter_context(patch.object(server, 'state_version', 0))
        stack.enter_context(patch.object(server, 'schedule'))
        stack.enter_context(patch.object(server, 'schedule_subscription'))
        stack.enter_context(patch.object(ranking, 'TERMS', dict(ranking.DEFAULT_TERMS)))
        self.sub = 'https://example.com/feed'
        server.FEEDS_FILE.write_text(self.sub)
        self.posts = [dict(url=f'https://example.com/{i}', title=f'Part {i}', first_seen=100,
                           published=i * 86400, estimated_minutes=10) for i in (3, 2, 1)]
        server.subscriptions[self.sub] = {'title': 'Example', 'items': self.posts}
        self.url = self.posts[1]['url']

    def page(self, url=None, limit=300, **kwargs):
        return server.feed('score', limit, post_url=url or self.url, **kwargs)

    def urls(self, items):
        return [p['url'] for p in items]

    def test_subscription_feed_is_scoped_before_ranking_and_limiting(self):
        other = 'https://other.example/feed'
        server.FEEDS_FILE.write_text(self.sub + '\n' + other)
        server.subscriptions[other] = {'title': 'Other', 'items': [dict(self.posts[0], url='https://other.example/post')]}
        # A link also present in the subscription must still appear on its page.
        server.LINKS_FILE.write_text(self.posts[0]['url'])
        page = server.feed('score', 2, subscription_url=self.sub)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len(page['items']), 2)
        self.assertTrue(all(p['sub'] == self.sub for p in page['items']))
        full = server.feed('score', 300, subscription_url=self.sub)
        self.assertEqual(set(self.urls(full['items'])), set(self.urls(self.posts)))
        self.assertEqual(server.viewed, {})

    def test_subscription_feed_missing_and_empty(self):
        self.assertEqual(server.feed('score', 300, subscription_url='missing'),
                         {'error': 'Subscription not found'})
        server.subscriptions[self.sub]['items'] = []
        page = server.feed('score', 300, subscription_url=self.sub)
        self.assertEqual(page['total'], 0)
        self.assertEqual(page['items'], [])

    def test_search_filters_before_limit_and_keeps_caches_separate(self):
        self.posts[2]['description'] = 'Rare Needle'
        result = server.feed('score', 1, search='rare NEEDLE')
        self.assertEqual(result['total'], 1)
        self.assertEqual(self.urls(result['items']), [self.posts[2]['url']])
        self.assertEqual(self.page(search='rare NEEDLE')['item']['url'], self.url)
        self.assertEqual(server.feed('score', 1, search='rare NEEDLE')['total'], 1)
        self.assertEqual(server.feed('score', 1)['total'], 3)
        self.assertEqual(server.feed('score', 1, search='absent')['total'], 0)
        self.assertEqual(server.feed('score', 1, subscription_url=self.sub, search='needle')['total'], 1)

    def test_post_search_keeps_target_and_filters_related_posts(self):
        page = self.page(search='Part 1', limit=1)
        self.assertEqual(page['item']['url'], self.url)
        self.assertEqual(self.urls(page['series'][0]['items']), [self.posts[2]['url']])
        self.assertEqual(page['similar']['total'], 1)
        self.assertEqual(self.urls(page['similar']['items']), [self.posts[2]['url']])

    def test_subscription_stats_cover_all_posts_and_current_view_state(self):
        server.viewed[self.posts[0]['url']] = 200
        server.viewed[self.posts[1]['url']] = 100
        server.skipped[self.posts[1]['url']] = 300
        server.colors[self.posts[0]['url']] = 'green'
        server.colors[self.posts[2]['url']] = 'red'
        stats = server.subscription_status()[0]
        self.assertEqual((stats['opened'], stats['skipped'], stats['unseen']), (1, 1, 1))
        self.assertEqual(stats['color_counts'], {'green': 1, 'white': 1, 'red': 1})
        self.assertEqual(stats['last_viewed'], 300)
        self.assertEqual(stats['next_up'], 0)
        server.viewed[self.posts[1]['url']] = 400
        stats = server.subscription_status()[0]
        self.assertEqual((stats['opened'], stats['skipped']), (2, 0))
        self.assertEqual(stats['next_up'], 1)
        server.subscriptions[self.sub]['items'] = []
        stats = server.subscription_status()[0]
        self.assertEqual(stats['unseen'], 0)
        self.assertIsNone(stats['last_viewed'])

    def test_subscription_next_up_view_across_series(self):
        s1 = [dict(url=f'https://example.com/s1/{i}', series='S1', episode=[1, i],
                   title=f'Show 1 Ep {i}', first_seen=100, published=i * 100) for i in (3, 2, 1)]
        s2 = [dict(url=f'https://example.com/s2/{i}', series='S2', episode=[1, i],
                   title=f'Show 2 Ep {i}', first_seen=100, published=i * 100) for i in (2, 1)]
        server.subscriptions[self.sub]['items'] = s1 + s2

        page = server.feed('score', 300, subscription_url=self.sub)
        self.assertEqual(page['next_up'], [])

        server.viewed[s1[2]['url']] = 100  # Ep 1
        server.viewed[s2[1]['url']] = 110  # Ep 1
        server.ranked_cache.clear()

        page = server.feed('score', 300, subscription_url=self.sub)
        self.assertEqual(set(self.urls(page['next_up'])), {s1[1]['url'], s2[0]['url']})
        for p in page['next_up']:
            self.assertTrue(any('next up' in r['label'] for r in p['reasons']))

        limited = server.feed('score', 1, subscription_url=self.sub)
        self.assertEqual(len(limited['items']), 1)
        self.assertEqual(set(self.urls(limited['next_up'])), {s1[1]['url'], s2[0]['url']})

        searched = server.feed('score', 10, subscription_url=self.sub, search='Show 1')
        self.assertEqual(self.urls(searched['next_up']), [s1[1]['url']])

        server.skipped[s1[1]['url']] = 200
        server.ranked_cache.clear()
        page = server.feed('score', 300, subscription_url=self.sub)
        self.assertEqual(self.urls(page['next_up']), [s2[0]['url']])

        server.FEEDS_FILE.write_text(self.sub + ' latest')
        server.ranked_cache.clear()
        page = server.feed('score', 300, subscription_url=self.sub)
        self.assertEqual(page['next_up'], [])

    def test_numbered_series_order_and_similarity(self):
        page = self.page()
        self.assertEqual(page['item']['url'], self.url)
        self.assertEqual(self.urls(page['series'][0]['items']), self.urls(self.posts[::-1]))
        self.assertEqual(page['series'][0]['total'], 3)
        self.assertNotIn(self.url, self.urls(page['similar']['items']))
        self.assertEqual(page['similar']['total'], 2)
        for p in page['similar']['items']:
            self.assertEqual(p['related_percent'], ranking.similarity(page['item'], p, ranking.TERMS)[0])
        self.assertEqual(server.viewed, {})  # Browsing the page does not open its source.

    def test_overlapping_playlists_keep_imported_order(self):
        for p in self.posts:
            p['youtube'] = p['url'].rsplit('/', 1)[-1]
        server.subscriptions[self.sub]['youtube_playlists'] = [
            dict(id='PLone', videos=['2', '1', '3', 'missing']),
            dict(id='PLtwo', videos=['3', '2']),
        ]
        page = self.page()
        self.assertEqual(len(page['series']), 2)
        self.assertEqual(self.urls(page['series'][0]['items']), [self.url, self.posts[2]['url'], self.posts[0]['url']])
        self.assertEqual(self.urls(page['series'][1]['items']), [self.posts[0]['url'], self.url])
        self.assertIn('PLone', page['series'][0]['url'])

    def test_explicit_episode_series_and_no_series(self):
        for i, p in enumerate(self.posts):
            p.update(series='show', episode=[1, 3 - i])
        page = self.page()
        self.assertEqual(self.urls(page['series'][0]['items']), self.urls(self.posts[::-1]))
        self.posts[1]['no_series'] = True
        server.ranked_cache.clear()
        self.assertEqual(self.page()['series'], [])

    def test_nonseries_youtube_posts_do_not_use_numbered_titles(self):
        for p in self.posts:
            p['youtube'] = p['url']
        self.assertEqual(self.page()['series'], [])

    def test_similar_posts_use_current_weights_and_exclude_other_subscriptions(self):
        other_sub = 'https://other.example/feed'
        server.FEEDS_FILE.write_text(self.sub + '\n' + other_sub)
        server.subscriptions[other_sub] = {'title': 'Other', 'items': [dict(self.posts[0], url='https://other.example/post')]}
        self.posts[0]['estimated_minutes'] = 20
        terms = dict(ranking.TERMS, similar_base_percent=0, similar_publish_weight=0, similar_length_weight=1)
        page = self.page(terms=terms)
        self.assertEqual(self.urls(page['similar']['items']), [self.posts[2]['url'], self.posts[0]['url']])
        self.assertEqual([p['related_percent'] for p in page['similar']['items']], [100, 50])
        terms['similar_length_weight'] = 0
        self.assertEqual(self.page(terms=terms)['similar'], {'items': [], 'total': 0})

    def test_limits_do_not_hide_target_or_memberships(self):
        page = self.page(url=self.posts[2]['url'], limit=1)
        self.assertEqual(page['item']['url'], self.posts[2]['url'])
        self.assertEqual(len(page['series'][0]['items']), 1)
        self.assertEqual(page['series'][0]['total'], 3)
        self.assertEqual(len(page['similar']['items']), 1)
        self.assertEqual(page['similar']['total'], 2)
        self.assertEqual(len(self.page(limit=3)['series'][0]['items']), 3)

    def test_scores_and_view_state_do_not_change_cached_main_feed(self):
        before = copy.deepcopy(server.feed('score'))
        page = self.page()
        self.assertEqual(page['item']['score'], ranking.TERMS['series_penalty'])
        self.assertEqual(server.feed('score'), before)
        server.set_viewed(self.url)
        page = self.page()
        self.assertIsNotNone(page['item']['last_viewed'])
        for group in page['series']:
            own = next(p for p in group['items'] if p['url'] == self.url)
            self.assertEqual(own['last_viewed'], page['item']['last_viewed'])
            self.assertEqual(own['score'], page['item']['score'])

    def test_saved_link_has_card_and_empty_related_feeds(self):
        link = 'https://example.com/saved?x=1&y=2#fragment'
        server.LINKS_FILE.write_text(link)
        server.cache[link] = dict(url=link, title='Saved')
        page = self.page(url=link)
        self.assertEqual(page['item']['title'], 'Saved')
        self.assertEqual(page['series'], [])
        self.assertEqual(page['similar']['items'], [])

    def test_enrichment_happens_only_when_requested(self):
        with patch.object(server, 'enrich_posts') as enrich:
            self.page()
            enrich.assert_not_called()
            self.page(enrich=True)
            self.assertEqual(enrich.call_args.args[0][0]['url'], self.url)
            self.assertEqual(enrich.call_args.args[1], ranking.TERMS['enrich_count'])

    def test_http_routes_validation_and_etag(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.ranker = 'score'
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f'http://127.0.0.1:{httpd.server_port}'
        query = '/api/post?' + urllib.parse.urlencode({'url': self.url, 'limit': 1})
        with urllib.request.urlopen(base + query) as response:
            tag = response.headers['ETag']
            page = json.load(response)
        self.assertEqual(page['item']['url'], self.url)
        self.assertEqual(len(page['series'][0]['items']), 1)
        request = urllib.request.Request(base + query, headers={'If-None-Match': tag})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 304)
        for path, code in [('/api/post', 400), ('/api/post?url=missing', 404),
                           ('/api/post?url=x&ranker=missing', 400), ('/api/post?url=x&terms={}', 400)]:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            self.assertEqual(error.exception.code, code)
        with urllib.request.urlopen(base + '/post?' + urllib.parse.urlencode({'url': self.url})) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('Post & related', response.read().decode())
        sub_query = '/api/subscription?' + urllib.parse.urlencode({'url': self.sub, 'view': 'next-up'})
        with urllib.request.urlopen(base + sub_query) as response:
            page = json.load(response)
        self.assertIn('next_up', page)
        self.assertEqual(page['items'], page['next_up'])
        with urllib.request.urlopen(base + '/subscription/next-up?' + urllib.parse.urlencode({'url': self.sub})) as response:
            self.assertEqual(response.status, 200)
            self.assertIn('Next up', response.read().decode())
