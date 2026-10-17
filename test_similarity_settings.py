"""Persistence and validation for normalized similarity settings."""

import contextlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ranking
import server


class SimilaritySettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'settings.json'
        for target, key, value in [(server, 'SETTINGS_FILE', self.path),
                                   (server, 'state_version', 0),
                                   (ranking, 'WEIGHTS', dict(ranking.DEFAULT_WEIGHTS))]:
            patcher = patch.object(target, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_relative_weights_round_trip_including_zero_and_fractional_values(self):
        for publish, length in [(1, 1), (0.25, 0.75), (0, 1), (0, 0)]:
            with self.subTest(publish=publish, length=length):
                weights = dict(ranking.DEFAULT_WEIGHTS, similar_publish_weight=publish,
                               similar_length_weight=length)
                self.assertIsNone(server.save_settings(weights))
                self.assertEqual(json.loads(self.path.read_text())['weights'], weights)
                ranking.WEIGHTS.update(ranking.DEFAULT_WEIGHTS)
                server.load_settings()
                self.assertEqual(server.settings()['weights'], weights)

    def test_older_settings_keep_new_defaults_and_existing_preferences(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_days=7, similar_penalty=60)
        del weights['similar_publish_weight']
        del weights['similar_length_weight']
        self.path.write_text(json.dumps({'weights': weights}))
        server.load_settings()
        self.assertEqual(ranking.WEIGHTS['similar_days'], 7)
        self.assertEqual(ranking.WEIGHTS['similar_penalty'], 60)
        self.assertEqual(ranking.WEIGHTS['similar_publish_weight'], 1)
        self.assertEqual(ranking.WEIGHTS['similar_length_weight'], 0)

    def test_invalid_weights_leave_saved_and_active_settings_unchanged(self):
        original = dict(ranking.DEFAULT_WEIGHTS)
        self.assertIsNone(server.save_settings(original))
        saved = self.path.read_text()
        version = server.state_version
        for key in ['similar_publish_weight', 'similar_length_weight']:
            self.assertIsNotNone(server.save_settings(dict(original, **{key: -1})))
            self.assertEqual(ranking.WEIGHTS, original)
            self.assertEqual(self.path.read_text(), saved)
            self.assertEqual(server.state_version, version)

    def test_running_settings_and_preview_endpoints(self):
        root = self.path.parent
        sub = 'https://example.com/feed'
        now = time.time()
        posts = [
            {'url': 'https://example.com/anchor', 'title': 'Anchor',
             'published': now, 'first_seen': now, 'estimated_minutes': 10},
            {'url': 'https://example.com/candidate', 'title': 'Candidate',
             'published': now - 15 * 86400, 'first_seen': now, 'estimated_minutes': 40},
        ]
        with contextlib.ExitStack() as stack:
            for name in ['LINKS_FILE', 'FEEDS_FILE', 'ADDED_FILE']:
                stack.enter_context(patch.object(server, name, root / name))
            server.FEEDS_FILE.write_text(sub)
            for name in ['cache', 'added', 'viewed', 'skipped', 'colors', 'ranked_cache']:
                stack.enter_context(patch.object(server, name, {}))
            server.viewed[posts[0]['url']] = now
            stack.enter_context(patch.object(server, 'subscriptions', {sub: {'title': 'Example', 'items': posts}}))
            stack.enter_context(patch.object(server, 'pending', set()))
            stack.enter_context(patch.object(server, 'schedule'))
            stack.enter_context(patch.object(server, 'schedule_subscription'))
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.ranker = 'score'
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{httpd.server_port}'

            def request(path, weights=None):
                data = json.dumps({'weights': weights}).encode() if weights is not None else None
                req = urllib.request.Request(base + path, data=data,
                                             headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)

            def candidate(query=''):
                return next(it for it in request('/api/feed' + query)['items']
                            if it['url'] == posts[1]['url'])

            try:
                defaults = request('/api/settings')['defaults']
                self.assertEqual(defaults['similar_publish_weight'], 1)
                self.assertEqual(defaults['similar_length_weight'], 0)
                self.assertEqual(candidate()['similar']['percent'], 50)
                equal = dict(defaults, similar_length_weight=1)
                self.assertTrue(request('/api/settings', equal)['ok'])
                self.assertEqual(candidate()['similar']['percent'], 38)
                preview = dict(equal, similar_publish_weight=0)
                query = '?weights=' + urllib.parse.quote(json.dumps(preview))
                self.assertEqual(candidate(query)['similar']['percent'], 25)
                # Previewing changes neither the saved settings nor the next normal result.
                self.assertEqual(request('/api/settings')['weights'], equal)
                self.assertEqual(candidate()['similar']['percent'], 38)
                invalid = dict(equal, similar_length_weight=-1)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/settings', invalid)
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(request('/api/settings')['weights'], equal)
                off = dict(equal, similar_publish_weight=0, similar_length_weight=0)
                self.assertTrue(request('/api/settings', off)['ok'])
                self.assertTrue(all(it['similar'] is None for it in request('/api/feed')['items']))
                with urllib.request.urlopen(base + '/', timeout=5) as response:
                    html = response.read().decode()
                self.assertIn('label: "Publish time similarity"', html)
                self.assertIn('label: "Content length similarity"', html)
                self.assertIn('term: "similarity"', html)
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()


    def test_old_repeat_amount_migrates_to_above_similarity_penalty(self):
        for amount in [0, 0.5, 75]:
            weights = dict(ranking.DEFAULT_WEIGHTS, repeat_per_post=amount)
            del weights['above_similarity_penalty']
            self.path.write_text(json.dumps({'weights': weights}))
            server.load_settings()
            self.assertEqual(ranking.WEIGHTS['above_similarity_penalty'], amount)
            self.assertNotIn('repeat_per_post', server.settings()['weights'])

    def test_explicit_new_penalty_wins_over_legacy_amount(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, repeat_per_post=100, above_similarity_penalty=3)
        self.path.write_text(json.dumps({'weights': weights}))
        server.load_settings()
        self.assertEqual(ranking.WEIGHTS['above_similarity_penalty'], 3)
