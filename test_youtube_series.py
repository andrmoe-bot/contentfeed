import io
import json
import unittest
from unittest.mock import patch

import feeds
import server


class PlaylistTests(unittest.TestCase):
    def test_import_channel_playlists_and_classic_video_order(self):
        def page(data):
            return io.BytesIO(('var ytInitialData = ' + json.dumps(data) + ';</script>'
                               '"INNERTUBE_CLIENT_VERSION":"1"').encode())
        listing = {"gridPlaylistRenderer": {"playlistId": "PLseries"}}
        videos = {"contents": [{"playlistVideoRenderer": {"videoId": v, "title": {"simpleText": v}}}
                               for v in ("b", "a")]}
        with patch.object(feeds, "open_url", side_effect=[page(listing), page(videos)]):
            result = feeds.channel_playlists('https://www.youtube.com/feeds/videos.xml?channel_id=UCabc')
        self.assertEqual(result, [{"id": "PLseries", "videos": ["b", "a"]}])

    def test_playlist_continuations_are_imported(self):
        initial = {"lockupViewModel": {"contentId": "a", "contentType": "LOCKUP_CONTENT_TYPE_VIDEO"},
                   "continuationCommand": {"token": "next"}}
        more = {"lockupViewModel": {"contentId": "b", "contentType": "LOCKUP_CONTENT_TYPE_VIDEO"}}
        page = io.BytesIO(('var ytInitialData = ' + json.dumps(initial) + ';</script>'
                           '"INNERTUBE_CLIENT_VERSION":"1"').encode())
        with patch.object(feeds, "open_url", return_value=page), \
             patch.object(feeds.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(more).encode())):
            result = feeds.channel_playlists('https://www.youtube.com/feeds/videos.xml?playlist_id=PL')
        self.assertEqual(result, [{"id": "PL", "videos": ["a", "b"]}])

    def test_playlist_order_replaces_title_detection(self):
        posts = [{"url": v, "youtube": v, "title": title} for v, title in
                 (("a", "Part 1"), ("b", "Unrelated title"), ("c", "Part 2"))]
        subscription = {"items": posts, "youtube_playlists": [{"id": "PL", "videos": ["b", "a"]}]}
        with patch.object(server, "subscriptions", {"channel": subscription}), \
             patch.object(server, "opened_at", return_value=None), \
             patch.object(server, "seen_at", return_value=None):
            nxt, unstarted = server.series_state(["channel"])
            self.assertEqual(unstarted, {"a": "Unrelated title"})
            with patch.object(server, "opened_at", side_effect=lambda url: 10 if url == "b" else None), \
                 patch.object(server, "view_state", return_value={"skipped": False}):
                nxt, _ = server.series_state(["channel"])
                self.assertEqual(nxt, {"a": "Unrelated title"})

    def test_existing_channels_are_backfilled(self):
        entry = {"older_loaded": 1, "feed_url": 'https://www.youtube.com/feeds/videos.xml?channel_id=UCabc'}
        with patch.object(server, "subscriptions", {"channel": entry}), \
             patch.object(server, "pending", set()), patch.object(server.executor, "submit") as submit:
            server.schedule_older("channel")
            submit.assert_called_once_with(server.load_older, "channel")


class SeriesStateTests(unittest.TestCase):
    def state(self, playlists, *, viewed=None, skipped=None, ids=('a', 'b', 'c', 'd')):
        posts = [{"url": v, "youtube": v, "title": v} for v in ids]
        with patch.object(server, 'subscriptions', {'channel': {
                'items': posts, 'youtube_playlists': [dict(id=str(i), videos=p) for i, p in enumerate(playlists)]}}), \
             patch.object(server, 'viewed', viewed or {}), \
             patch.object(server, 'skipped', skipped or {}), patch.object(server, 'colors', {}):
            return server.series_state(['channel'])

    def test_overlapping_playlists_offer_both_successors(self):
        self.assertEqual(self.state([['a', 'b'], ['a', 'c']], viewed={'a': 1})[0], {'b': 'a', 'c': 'a'})

    def test_started_playlist_overrides_unstarted_overlap(self):
        for playlists in ([['c', 'b'], ['a', 'b']], [['a', 'b'], ['c', 'b']]):
            nxt, held = self.state(playlists, viewed={'a': 1})
            self.assertEqual(nxt, {'b': 'a'})
            self.assertNotIn('b', held)

    def test_skips_release_penalty_without_creating_next_up(self):
        self.assertEqual(self.state([['a', 'b', 'c']], skipped={'a': 1}), ({}, {}))

    def test_skipped_successor_is_not_next_up(self):
        self.assertEqual(self.state([['a', 'b', 'c']], viewed={'a': 1}, skipped={'b': 2})[0], {})

    def test_last_episode_has_no_successor(self):
        self.assertEqual(self.state([['a', 'b']], viewed={'b': 2})[0], {})

    def test_most_recent_open_controls_successor(self):
        self.assertEqual(self.state([['a', 'b', 'c']], viewed={'a': 3, 'b': 2})[0], {'b': 'a'})

    def test_missing_video_ids_are_ignored(self):
        self.assertEqual(self.state([['missing', 'a', 'unavailable', 'b']], viewed={'a': 1})[0], {'b': 'a'})

    def test_empty_and_single_video_playlists_have_no_penalty(self):
        self.assertEqual(self.state([[], ['a']]), ({}, {}))

    def test_unassigned_videos_use_publication_order_for_next_up(self):
        self.assertEqual(self.state([], viewed={'c': 1})[0], {'b': 'c'})

    def test_non_youtube_title_detection_still_works(self):
        posts = [{'url': v, 'title': 'Part ' + str(i)} for i, v in enumerate(('a', 'b'), 1)]
        with patch.object(server, 'subscriptions', {'feed': {'items': posts}}), \
             patch.object(server, 'viewed', {}), patch.object(server, 'skipped', {}):
            self.assertEqual(server.series_state(['feed'])[1], {'b': 'Part 1'})


class ImportLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.entry = {'feed_url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UCabc', 'items': []}
        self.subs = {'channel': self.entry}
        self.patches = [patch.object(server, 'subscriptions', self.subs),
                        patch.object(server, 'pending', {'older:channel'}),
                        patch.object(server, 'save_json'),
                        patch.object(feeds, 'older_videos', return_value=[]),
                        patch.object(feeds, 'channel_playlists', return_value=[{'id': 'PL', 'videos': ['a']}])]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    def test_playlist_failure_retains_history_and_retry_skips_history(self):
        self.mocks[3].return_value = [{'url': 'a', 'youtube': 'a', 'published': 1}]
        self.mocks[4].side_effect = feeds.FeedError('offline')
        server.load_older('channel')
        self.assertEqual(self.entry['items'][0]['url'], 'a')
        self.assertIn('older_loaded', self.entry)
        self.assertNotIn('playlists_loaded', self.entry)
        self.assertIn('older_error', self.entry)
        self.assertNotIn('older:channel', server.pending)
        self.mocks[4].side_effect = None
        server.load_older('channel')
        self.assertEqual(self.mocks[3].call_count, 1)
        self.assertIn('playlists_loaded', self.entry)
        self.assertNotIn('older_error', self.entry)

    def test_completed_import_is_not_downloaded_again(self):
        server.load_older('channel')
        server.load_older('channel')
        self.assertEqual(self.mocks[3].call_count, 1)
        self.assertEqual(self.mocks[4].call_count, 1)

    def test_unsubscribe_during_import_does_not_recreate_subscription(self):
        self.mocks[4].side_effect = lambda _: self.subs.clear() or []
        server.load_older('channel')
        self.assertEqual(self.subs, {})
        self.assertNotIn('older:channel', server.pending)

    def test_failed_import_is_throttled(self):
        self.entry['older_error'] = {'time': server.time.time()}
        server.pending.clear()
        with patch.object(server.executor, 'submit') as submit:
            server.schedule_older('channel')
        submit.assert_not_called()

    def test_backfill_does_not_reload_history(self):
        self.entry['older_loaded'] = 1
        server.load_older('channel')
        self.mocks[3].assert_not_called()
        self.assertEqual(self.entry['youtube_playlists'][0]['id'], 'PL')


class ParserEdgeTests(unittest.TestCase):
    def page(self, data):
        return io.BytesIO(('var ytInitialData = ' + json.dumps(data) + ';</script>'
                           '"INNERTUBE_CLIENT_VERSION":"1"').encode())

    def test_channel_listing_paginates_deduplicates_and_excludes_uploads(self):
        listing = {'contents': [{'gridPlaylistRenderer': {'playlistId': p}} for p in ('UUabc', 'PL1', 'PL1')],
                   'continuationCommand': {'token': 'next'}}
        more = {'playlistRenderer': {'playlistId': 'PL2'}}
        calls = []
        def open_page(url, accept):
            calls.append(url)
            return self.page(listing if '/channel/' in url else {})
        with patch.object(feeds, 'open_url', side_effect=open_page), \
             patch.object(feeds.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(more).encode())):
            result = feeds.channel_playlists('https://www.youtube.com/feeds/videos.xml?channel_id=UCabc')
        self.assertEqual([p['id'] for p in result], ['PL1', 'PL2'])
        self.assertEqual(len(calls), 3)

    def test_invalid_page_and_network_errors_are_retryable(self):
        url = 'https://www.youtube.com/feeds/videos.xml?playlist_id=PL'
        with patch.object(feeds, 'open_url', return_value=io.BytesIO(b'consent page')):
            with self.assertRaises(feeds.FeedError):
                feeds.channel_playlists(url)
        with patch.object(feeds, 'open_url', side_effect=OSError('offline')):
            with self.assertRaises(feeds.FeedError):
                feeds.channel_playlists(url)

    def test_duplicate_and_unavailable_videos(self):
        data = {'contents': [{'playlistVideoRenderer': {'videoId': v}} for v in ('a', 'a', 'b')]
                + [{'playlistVideoRenderer': {'title': {'simpleText': 'Deleted video'}}}]}
        with patch.object(feeds, 'open_url', return_value=self.page(data)):
            result = feeds.channel_playlists('https://www.youtube.com/feeds/videos.xml?playlist_id=PL')
        self.assertEqual(result[0]['videos'], ['a', 'b'])

    def test_repeated_continuation_token_terminates(self):
        data = {'continuationCommand': {'token': 'loop'}}
        with patch.object(feeds, 'open_url', return_value=self.page(data)), \
             patch.object(feeds.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(data).encode())) as fetch:
            self.assertEqual(feeds.channel_playlists('https://www.youtube.com/feeds/videos.xml?playlist_id=PL'),
                             [{'id': 'PL', 'videos': []}])
        self.assertEqual(fetch.call_count, 1)

    def test_non_youtube_subscription_does_not_make_requests(self):
        with patch.object(feeds, 'open_url') as fetch:
            self.assertEqual(feeds.channel_playlists('https://example.com/feed'), [])
        fetch.assert_not_called()


import test_server as server_tests


class PlaylistIntegrationTests(server_tests.FeedCacheTests):
    def setUp(self):
        super().setUp()
        server.ranking.WEIGHTS['above_similarity_penalty'] = 0
        server.ranking.WEIGHTS['similar_penalty'] = 0

    def test_saved_playlist_changes_scores_and_survives_feed_refresh(self):
        import ranking
        first = dict(self.post, youtube='a', title='First', url='https://www.youtube.com/watch?v=a')
        second = dict(self.post, youtube='b', title='Second', url='https://www.youtube.com/watch?v=b')
        self.post = first
        entry = server.subscriptions[self.url]
        entry.update(feed_url='https://www.youtube.com/feeds/videos.xml?channel_id=UCabc', items=[second, first])
        # Populate the ranking cache before import; importing must invalidate it.
        before = server.feed('score')
        self.assertFalse(any(p.get('series_first') for p in before['items']))
        with patch.object(feeds, 'older_videos', return_value=[]), \
             patch.object(feeds, 'channel_playlists', return_value=[{'id': 'PL', 'videos': ['a', 'b']}]):
            server.load_older(self.url)
        saved = json.loads(server.FEED_CACHE_FILE.read_text())
        self.assertEqual(saved[self.url]['youtube_playlists'], [{'id': 'PL', 'videos': ['a', 'b']}])
        ranked = {p['youtube']: p for p in server.feed('score')['items']}
        self.assertEqual(ranked['a']['youtube_playlists'], ['PL'])
        self.assertEqual(ranked['b']['youtube_playlists'], ['PL'])
        self.assertEqual(ranked['b']['series_first'], 'First')
        self.assertEqual(ranked['b']['score'], ranked['a']['score'] + ranking.WEIGHTS['series_penalty'])
        self.refresh({'items': [dict(second)], 'title': 'Example'})
        self.assertEqual(entry['youtube_playlists'], server.subscriptions[self.url]['youtube_playlists'])
        server.viewed[first['url']] = 100
        server.state_version += 1
        ranked = {p['youtube']: p for p in server.feed('score')['items']}
        self.assertEqual(ranked['b']['next_after'], 'First')
        self.assertIsNone(ranked['b']['series_first'])

    def test_latest_tag_disables_playlist_score_adjustments(self):
        first = dict(self.post, youtube='a', title='First', url='https://www.youtube.com/watch?v=a')
        second = dict(self.post, youtube='b', title='Second', url='https://www.youtube.com/watch?v=b')
        server.FEEDS_FILE.write_text(self.url + ' latest')
        server.subscriptions[self.url].update(items=[second, first], youtube_playlists=[{'id': 'PL', 'videos': ['a', 'b']}])
        ranked = {p['youtube']: p for p in server.feed('score')['items']}
        self.assertEqual(ranked['a']['score'], ranked['b']['score'])
