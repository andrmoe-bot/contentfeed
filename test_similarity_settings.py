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
                                   (ranking, 'TERMS', dict(ranking.DEFAULT_TERMS))]:
            patcher = patch.object(target, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_subscription_base_gets_default_and_saved_value_round_trips(self):
        terms = dict(ranking.DEFAULT_TERMS)
        del terms['similar_base_percent']
        self.path.write_text(json.dumps({'terms': terms}))
        server.load_settings()
        self.assertEqual(ranking.TERMS['similar_base_percent'], 50)
        for base in [0, 12.5, 50, 100]:
            self.assertIsNone(server.save_settings(dict(ranking.TERMS, similar_base_percent=base)))
            server.load_settings()
            self.assertEqual(ranking.TERMS['similar_base_percent'], base)

    def test_relative_weights_round_trip_including_zero_and_fractional_values(self):
        for publish, length, title in [(1, 1, 1), (0.25, 0.75, 0.5), (0, 1, 0), (0, 0, 1), (0, 0, 0)]:
            with self.subTest(publish=publish, length=length, title=title):
                terms = dict(ranking.DEFAULT_TERMS, similar_publish_weight=publish,
                               similar_length_weight=length, similar_title_weight=title)
                self.assertIsNone(server.save_settings(terms))
                self.assertEqual(json.loads(self.path.read_text())['terms'], terms)
                ranking.TERMS.update(ranking.DEFAULT_TERMS)
                server.load_settings()
                self.assertEqual(server.settings()['terms'], terms)

    def test_older_settings_keep_new_defaults_and_existing_preferences(self):
        terms = dict(ranking.DEFAULT_TERMS, similar_days=7, similar_penalty=60)
        del terms['similar_publish_weight']
        del terms['similar_length_weight']
        del terms['similar_title_weight']
        self.path.write_text(json.dumps({'terms': terms}))
        server.load_settings()
        self.assertEqual(ranking.TERMS['similar_days'], 7)
        self.assertEqual(ranking.TERMS['similar_penalty'], 60)
        self.assertEqual(ranking.TERMS['similar_publish_weight'], 1)
        self.assertEqual(ranking.TERMS['similar_length_weight'], 0)
        self.assertEqual(ranking.TERMS['similar_title_weight'], 0)

    def test_invalid_weights_leave_saved_and_active_settings_unchanged(self):
        original = dict(ranking.DEFAULT_TERMS)
        self.assertIsNone(server.save_settings(original))
        saved = self.path.read_text()
        version = server.state_version
        for key in ['similar_publish_weight', 'similar_length_weight', 'similar_title_weight']:
            self.assertIsNotNone(server.save_settings(dict(original, **{key: -1})))
            self.assertEqual(ranking.TERMS, original)
            self.assertEqual(self.path.read_text(), saved)
            self.assertEqual(server.state_version, version)

    def test_running_settings_and_preview_endpoints(self):
        root = self.path.parent
        sub = 'https://example.com/feed'
        now = time.time()
        posts = [
            {'url': 'https://example.com/anchor', 'title': 'Building a CPU part 1',
             'published': now, 'first_seen': now, 'estimated_minutes': 10},
            {'url': 'https://example.com/candidate', 'title': 'Building a CPU part 2',
             'published': now - 15 * 86400, 'first_seen': now, 'estimated_minutes': 40},
        ]
        with contextlib.ExitStack() as stack:
            for name in ['LINKS_FILE', 'FEEDS_FILE', 'STATE_FILE']:
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

            def request(path, terms=None):
                data = json.dumps({'terms': terms}).encode() if terms is not None else None
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
                self.assertNotIn('event_bonus', defaults)  # events aren't feed items, so no event terms
                self.assertEqual(defaults['task_bonus'], 0)
                self.assertEqual(defaults['event_lead_minutes'], 30)
                self.assertEqual(defaults['task_deadline_bonus'], 200)
                self.assertEqual(candidate()['similar']['percent'], 75)
                equal = dict(defaults, similar_length_weight=1)
                self.assertTrue(request('/api/settings', equal)['ok'])
                self.assertEqual(candidate()['similar']['percent'], 69)
                preview = dict(equal, similar_publish_weight=0)
                query = '?terms=' + urllib.parse.quote(json.dumps(preview))
                self.assertEqual(candidate(query)['similar']['percent'], 62)
                # Previewing changes neither the saved settings nor the next normal result.
                self.assertEqual(request('/api/settings')['terms'], equal)
                self.assertEqual(candidate()['similar']['percent'], 69)
                title_only = dict(equal, similar_publish_weight=0, similar_length_weight=0, similar_title_weight=1)
                query = '?terms=' + urllib.parse.quote(json.dumps(title_only))
                self.assertEqual(candidate(query)['similar']['percent'], 88)
                all_three = dict(equal, similar_title_weight=1)
                query = '?terms=' + urllib.parse.quote(json.dumps(all_three))
                self.assertEqual(candidate(query)['similar']['percent'], 75)
                self.assertEqual(request('/api/settings')['terms'], equal)
                invalid = dict(equal, similar_length_weight=-1)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/settings', invalid)
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(request('/api/settings')['terms'], equal)
                off = dict(equal, similar_base_percent=0, similar_publish_weight=0, similar_length_weight=0)
                self.assertTrue(request('/api/settings', off)['ok'])
                self.assertTrue(all(it['similar'] is None for it in request('/api/feed')['items']))
                with urllib.request.urlopen(base + '/', timeout=5) as response:
                    html = response.read().decode()
                self.assertIn(f'ContentFeed · v{server.VERSION}', html)
                self.assertNotIn('{{VERSION}}', html)
                if server.COMMIT:
                    self.assertIn(f'v{server.VERSION} ({server.COMMIT})', html)
                    self.assertRegex(server.VERSION, r'^\d+\.\d+\.\d+$')  # major.minor.commits
                self.assertIn('<script src="/settings.js"></script>', html)
                with urllib.request.urlopen(base + '/settings.js', timeout=5) as response:
                    self.assertEqual(response.headers['Content-Type'], 'text/javascript; charset=utf-8')
                    html = response.read().decode()  # the score panel's settings
                self.assertIn('label: "Publish time similarity"', html)
                self.assertIn('label: "Content length similarity"', html)
                self.assertIn('label: "Title similarity"', html)
                self.assertNotIn('key: "event_bonus"', html)
                self.assertNotIn('past_event_penalty', html)
                self.assertIn('key: "event_lead_minutes", label: "Event lead time"', html)
                self.assertNotIn('event_urgency_bonus', html)
                self.assertIn('key: "task_bonus", label: "Task"', html)
                self.assertIn('term: "similarity"', html)
                with urllib.request.urlopen(base + '/subscriptions', timeout=5) as response:
                    self.assertIn(f'ContentFeed · v{server.VERSION}', response.read().decode())
                with urllib.request.urlopen(base + '/post?url=' + urllib.parse.quote(posts[0]['url']), timeout=5) as response:
                    self.assertIn(f'ContentFeed · v{server.VERSION}', response.read().decode())
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()


    def test_old_repeat_amount_migrates_to_above_similarity_penalty(self):
        for amount in [0, 0.5, 75]:
            terms = dict(ranking.DEFAULT_TERMS, repeat_per_post=amount)
            del terms['above_similarity_penalty']
            self.path.write_text(json.dumps({'terms': terms}))
            server.load_settings()
            self.assertEqual(ranking.TERMS['above_similarity_penalty'], amount)
            self.assertNotIn('repeat_per_post', server.settings()['terms'])

    def test_explicit_new_penalty_wins_over_legacy_amount(self):
        terms = dict(ranking.DEFAULT_TERMS, repeat_per_post=100, above_similarity_penalty=3)
        self.path.write_text(json.dumps({'terms': terms}))
        server.load_settings()
        self.assertEqual(ranking.TERMS['above_similarity_penalty'], 3)
