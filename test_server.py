import contextlib
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ranking
import server


class FeedCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("LINKS_FILE", "FEEDS_FILE", "CACHE_FILE", "FEED_CACHE_FILE",
                     "STATE_FILE", "SETTINGS_FILE", "JELLYFIN_FILE", "CALDAV_FILE"):
            self.stack.enter_context(patch.object(server, name, root / name, create=True))
        for name in ("subscriptions", "cache", "added", "viewed", "colors", "ranked_cache"):
            self.stack.enter_context(patch.object(server, name, {}))
        self.stack.enter_context(patch.object(server, "pending", set()))
        self.stack.enter_context(patch.object(server, "state_version", 0))
        self.stack.enter_context(patch.object(ranking, "TERMS", dict(ranking.DEFAULT_TERMS)))
        # One mode, on whatever the calendar holds, whose settings are ranking.TERMS.
        self.stack.enter_context(patch.object(server, "modes", [{"name": "Normal", "when": dict.fromkeys(server.SITUATIONS, True),
                                                      "terms": ranking.TERMS}]))
        self.stack.enter_context(patch.object(server, "schedule", return_value=None))
        self.stack.enter_context(patch.object(server, "schedule_subscription", return_value=None))
        self.stack.enter_context(patch.object(server.jellyfin, "read_logins", return_value=({}, None)))
        self.stack.enter_context(patch.object(server.jellyfin, "login_for", return_value=None))
        self.url = "https://example.com/feed"
        self.post = {"url": "https://example.com/post", "title": "Example",
                     "first_seen": 1_000_000, "published": 1_000_000}
        server.FEEDS_FILE.write_text(self.url)
        server.subscriptions[self.url] = {"title": "Example", "feed_url": self.url,
                                           "items": [self.post], "fetched": 1}

    def refresh(self, fresh):
        with patch.object(server.feeds, "fetch", return_value=fresh):
            server.refresh_subscription(self.url)

    def test_pending_count_does_not_rerank(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            server.pending.add("fetch")
            self.assertEqual(server.feed("score")["pending"], 1)
            server.pending.clear()
            self.assertEqual(server.feed("score")["pending"], 0)
        self.assertEqual(rank.call_count, 1)

    def test_unchanged_refresh_preserves_cache(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            self.refresh(None)  # HTTP 304
            server.feed("score")
            self.refresh({"title": "Example", "items": [copy.deepcopy(self.post)]})
            server.feed("score")
        self.assertEqual(rank.call_count, 1)

    def test_known_rss_refresh_does_not_probe_caldav(self):
        with patch.object(server.caldav, "fetch", side_effect=AssertionError("unexpected CalDAV probe")):
            self.refresh({"title": "Example", "items": [copy.deepcopy(self.post)]})

    def test_caldav_subscription_does_not_fall_through_to_feed_discovery(self):
        url = "https://calendar.example/work/"
        event = {"url": url + "#item=event", "title": "Event", "published": 2_000_000,
                 "calendar_kind": "event", "calendar_start": 2_000_000, "deadline": None}
        fresh = {"title": "Work", "site": url, "items": [], "events": [event], "complete": True}
        with patch.object(server.caldav, "fetch", return_value=fresh), \
                patch.object(server.feeds, "discover") as discover:
            self.assertIsNone(server.subscribe(url))
        discover.assert_not_called()
        self.assertEqual(server.subscriptions[url]["events"], [event])
        self.assertEqual(server.subscriptions[url]["items"], [])
        self.assertGreater(server.subscriptions[url]["fetched"], 0)

    def test_source_is_found_once_and_saved(self):
        url = "https://calendar.example/work/"
        fresh = {"title": "Work", "site": url, "items": [], "events": [], "complete": True}
        with patch.object(server.caldav, "fetch", return_value=fresh) as fetch, \
                patch.object(server.feeds, "discover") as discover:
            self.assertIsNone(server.subscribe(url))
            server.refresh_subscription(url, force=True)
        self.assertEqual(server.subscriptions[url]["source"], "caldav")
        self.assertEqual(fetch.call_count, 2)
        discover.assert_not_called()

    def test_saved_subscriptions_get_their_source_from_what_was_saved(self):
        self.refresh({"title": "Example", "items": [copy.deepcopy(self.post)]})
        self.assertEqual(server.subscriptions[self.url]["source"], "feed")
        self.assertEqual(server.known_source("https://x.example/", {"page": True}), "page")
        self.assertEqual(server.known_source("https://x.example/", {"items": [{"url": "u", "jellyfin": True}]}), "jellyfin")
        self.assertEqual(server.known_source("https://x.example/", {"events": [{}]}), "caldav")
        self.assertEqual(server.known_source("https://tv.nrk.no/serie/skam", {}), "nrk")
        self.assertIsNone(server.known_source("https://x.example/", {}))

    def test_vanished_feed_falls_back_to_reading_the_page(self):
        page = {"title": "Example", "page": True, "items": [copy.deepcopy(self.post)]}
        with patch.object(server.feeds, "fetch", side_effect=server.feeds.NoFeed("gone")), \
                patch.object(server.pages, "fetch", return_value=page):
            server.refresh_subscription(self.url)
        self.assertEqual(server.subscriptions[self.url]["source"], "page")

    def test_caldav_login_is_loaded_server_side_and_passed_to_fetch(self):
        url = "https://calendar.example/work/"
        login = {"username": "reader", "password": "secret"}
        fresh = {"title": "Work", "site": url, "items": [], "complete": True}
        with patch.object(server.caldav, "read_logins", return_value=({url: login}, None)), \
                patch.object(server.caldav, "fetch", return_value=fresh) as fetch:
            self.assertIsNone(server.subscribe(url))
        fetch.assert_called_once_with(url, login)
        self.assertNotIn("secret", server.subscription_status()[0]["url"])

    def test_selected_calendar_urls_can_be_added_separately_with_parent_login(self):
        parent = "https://calendar.example/andreas/"
        urls = [parent + "work/", parent + "family/"]
        login = {"username": "reader", "password": "secret"}
        fresh = [{"title": name, "site": url, "items": [], "complete": True}
                 for url, name in zip(urls, ("Work", "Family"))]
        with patch.object(server.caldav, "read_logins", return_value=({parent: login}, None)), \
                patch.object(server.caldav, "fetch", side_effect=fresh) as fetch:
            for url in urls:
                self.assertIsNone(server.subscribe(url))
        self.assertEqual([call.args for call in fetch.call_args_list], [(urls[0], login), (urls[1], login)])
        self.assertEqual(server.FEEDS_FILE.read_text(), "\n".join((self.url, *urls)) + "\n")
        self.assertTrue(set(urls).issubset({entry["url"] for entry in server.subscription_status()}))

    def test_subscription_rejects_credentials_embedded_in_url(self):
        error = server.subscribe("https://reader:secret@calendar.example/work/")
        self.assertIn("caldav.json", error)

    def test_legacy_calendar_url_credentials_are_moved_out_of_feed_files(self):
        old_url = "https://reader:p%40ss@calendar.example/work/"
        clean_url = "https://calendar.example/work/"
        server.FEEDS_FILE.write_text(old_url + " home\n")
        server.subscriptions.clear()
        server.subscriptions[old_url] = {"title": "Work", "items": []}
        self.assertEqual(server.migrate_caldav_url_credentials(), 1)
        logins, problem = server.caldav.read_logins(server.CALDAV_FILE)
        self.assertIsNone(problem)
        self.assertEqual(logins[clean_url], {"username": "reader", "password": "p@ss"})
        self.assertEqual(server.CALDAV_FILE.stat().st_mode & 0o077, 0)
        self.assertEqual(server.FEEDS_FILE.read_text(), clean_url + " home\n")
        self.assertNotIn("p%40ss", server.FEEDS_FILE.read_text())
        self.assertNotIn("p%40ss", server.FEED_CACHE_FILE.read_text())
        self.assertIn(clean_url, server.subscriptions)
        self.assertNotIn(old_url, server.subscriptions)
        self.assertNotIn("p@ss", str(server.subscription_status()))

    def test_first_rss_subscription_falls_back_after_caldav_probe(self):
        url = "https://example.test/articles"
        fresh = {"title": "Articles", "feed_url": "https://example.test/articles.xml",
                 "items": [dict(self.post, url="https://example.test/post")]}
        with patch.object(server.caldav, "fetch", side_effect=server.caldav.CalDAVError("not CalDAV")), \
                patch.object(server.feeds, "discover", return_value=fresh["feed_url"]) as discover, \
                patch.object(server.feeds, "fetch", return_value=fresh) as fetch:
            self.assertIsNone(server.subscribe(url))
        discover.assert_called_once_with(url)
        fetch.assert_called_once_with(fresh["feed_url"])
        self.assertEqual(server.subscriptions[url]["title"], "Articles")

    def test_failed_refresh_preserves_cache(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            with patch.object(server.feeds, "fetch", side_effect=server.feeds.FeedError("offline")):
                server.refresh_subscription(self.url)
            server.feed("score")
        self.assertEqual(rank.call_count, 1)
        self.assertEqual(server.subscriptions[self.url]["error"], "offline")

    def test_changed_posts_or_title_invalidate_cache(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            self.refresh({"title": "Renamed", "items": [copy.deepcopy(self.post)]})
            self.assertEqual(server.feed("score")["items"][0]["feed"], "Renamed")
            changed = dict(self.post, title="Changed post")
            self.refresh({"title": "Renamed", "items": [changed]})
            self.assertEqual(server.feed("score")["items"][0]["title"], "Changed post")
        self.assertEqual(rank.call_count, 3)

    def test_view_and_settings_changes_invalidate_cache(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            self.assertTrue(server.set_viewed(self.post["url"]))
            self.assertIsNotNone(server.feed("score")["items"][0]["last_viewed"])
            self.assertIsNone(server.save_settings(dict(ranking.TERMS, similar_penalty=0)))
            self.assertIsNone(server.feed("score")["items"][0]["similar"])
        self.assertEqual(rank.call_count, 3)

    def test_expired_cache_reranks(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            for cached in server.ranked_cache.values():
                cached.built = 0  # age the entry, keep the rest
            server.feed("score")
        self.assertEqual(rank.call_count, 2)

    def test_per_item_files_move_into_state_json(self):
        data = server.STATE_FILE.parent
        (data / "added.json").write_text('{"a": 1}')
        (data / "viewed.json").write_text('{"a": 2, "b": 3}')
        (data / "skipped.json").write_text('{"b": 4}')
        (data / "colors.json").write_text('{"a": "green"}')
        server.load_state()
        self.assertEqual(server.load_json(server.STATE_FILE), {"version": 1, "items": {
            "a": {"added": 1, "viewed": 2, "color": "green"}, "b": {"viewed": 3, "color": "white"}}})
        self.assertFalse((data / "viewed.json").exists())
        self.assertTrue((data / "viewed.json.bak").exists())
        for values in (server.added, server.viewed, server.colors):
            values.clear()
        server.load_state()  # now from state.json
        self.assertEqual((server.added, server.viewed, server.colors),
                         ({"a": 1}, {"a": 2, "b": 3}, {"a": "green", "b": "white"}))

    def test_skips_in_state_json_become_maybe_later(self):
        """Only a skip still in effect, on an untagged item, becomes the white tag; the skip field goes."""
        server.save_json(server.STATE_FILE, {"version": 1, "items": {
            "skipped": {"skipped": 5}, "reopened": {"viewed": 6, "skipped": 5},
            "red": {"skipped": 5, "color": "red"}, "opened": {"viewed": 1}}})
        server.load_state()
        self.assertEqual(server.colors, {"skipped": "white", "red": "red"})
        self.assertEqual(server.viewed, {"reopened": 6, "opened": 1})
        saved = server.load_json(server.STATE_FILE)["items"]
        self.assertFalse(any("skipped" in fields for fields in saved.values()))

    def test_tags_only_set_the_tag(self):
        """No tag counts as opening the item; "none" removes the tag."""
        for tag in ranking.TAGS:
            self.assertTrue(server.set_color(self.post["url"], tag))
            self.assertEqual(server.colors[self.post["url"]], tag)
            self.assertNotIn(self.post["url"], server.viewed)
            self.assertEqual(server.load_json(server.STATE_FILE)["items"][self.post["url"]], {"color": tag})
        self.assertTrue(server.set_color(self.post["url"], "none"))
        self.assertNotIn(self.post["url"], server.colors)
        self.assertFalse(server.set_color(self.post["url"], "blue"))

    def test_saved_green_points_carry_over_only_if_changed(self):
        old = dict(ranking.DEFAULT_TERMS)
        for key in ("green_points", "white_points", "red_points"):
            del old[key]
        for green, expected in ((0, 10), (25, 25)):
            with self.subTest(green=green):
                server.save_json(server.SETTINGS_FILE, {"version": 1, "terms": dict(old, green_bonus_points=green)})
                server.load_settings()
                self.assertEqual((server.mode_terms("Normal")["green_points"], server.mode_terms("Normal")["white_points"], server.mode_terms("Normal")["red_points"]),
                                 (expected, -50, -1000))
                server.mode_terms("Normal").update(ranking.DEFAULT_TERMS)

    def test_settings_are_saved_with_their_version_and_served_with_limits(self):
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, task_bonus=5)))
        saved = server.load_json(server.SETTINGS_FILE)
        self.assertEqual(saved["version"], server.SETTINGS_VERSION)
        server.load_settings()  # task_bonus is the fixed term here, not the old deadline ramp
        self.assertEqual((server.mode_terms("Normal")["task_bonus"], server.mode_terms("Normal")["task_deadline_bonus"]), (5, 200))
        limits = server.settings()["limits"]
        self.assertEqual(limits["seen_bonus"], {"min": -ranking.LIMIT, "max": ranking.LIMIT, "integer": False})
        self.assertEqual(limits["enrich_count"], {"min": 0, "max": 100, "integer": True})

    def test_saved_settings_ignore_removed_tag_similarity(self):
        server.save_json(server.SETTINGS_FILE, {"terms": dict(ranking.DEFAULT_TERMS, tag_similarity=99)})
        server.load_settings()
        self.assertEqual(server.mode_terms("Normal"), ranking.DEFAULT_TERMS)

    def test_settings_saved_under_the_old_weights_key_still_load(self):
        server.save_json(server.SETTINGS_FILE, {"weights": dict(ranking.DEFAULT_TERMS, seen_bonus=-50)})
        server.load_settings()
        self.assertEqual(server.mode_terms("Normal")["seen_bonus"], -50)

    def test_old_calendar_urgency_values_migrate_to_renamed_settings(self):
        old = {key: value for key, value in ranking.DEFAULT_TERMS.items() if key != "task_deadline_bonus"}
        old.update(task_bonus=60, event_urgency_bonus=80)  # the removed event ramp is dropped
        server.save_json(server.SETTINGS_FILE, {"terms": old})
        server.load_settings()
        self.assertEqual(server.mode_terms("Normal")["task_deadline_bonus"], 60)
        self.assertEqual(server.mode_terms("Normal")["task_bonus"], 0)
        self.assertNotIn("event_urgency_bonus", server.mode_terms("Normal"))
        self.assertEqual(server.mode_terms("Normal")["event_lead_minutes"], 30)

    def test_state_changed_during_ranking_is_not_cached(self):
        original_rank = ranking.rank

        def rank_and_change(*args, **kwargs):
            result = original_rank(*args, **kwargs)
            server.save_state()
            return result

        with patch.object(ranking, "rank", side_effect=rank_and_change):
            server.feed("score")
        self.assertFalse(server.ranked_cache)

    def test_new_link_can_use_cache_after_added_time_is_saved(self):
        url = "https://example.com/link"
        server.LINKS_FILE.write_text(url)
        server.cache[url] = {"url": url, "title": "Link"}
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            server.feed("score")
        self.assertEqual(rank.call_count, 1)

    def test_pagination_resumes_cached_ranking_and_preserves_total(self):
        server.subscriptions[self.url]['items'] = [dict(self.post, url=f'https://example.com/{i}') for i in range(20)]
        with patch.object(ranking, 'rank', wraps=ranking.rank) as rank:
            first = server.feed('score', limit=2)
            cached = next(iter(server.ranked_cache.values())).ranked
            self.assertEqual(len(cached.items), 2)
            self.assertEqual(first['total'], 20)
            next_page = server.feed('score', limit=5)
            self.assertEqual(len(cached.items), 5)
            self.assertEqual(next_page['items'][:2], first['items'])
            self.assertEqual(server.feed('score', limit=1)['items'], first['items'][:1])
            self.assertEqual(next_page['total'], 20)
        rank.assert_called_once()

    def test_enrichment_beyond_visible_prefix_keeps_highest_incomplete_selection(self):
        ranking.TERMS["enrich_count"] = 5  # off by default
        server.subscriptions[self.url]['items'] = [
            dict(self.post, url=f'https://example.com/{i}', estimated_minutes=10 if i < 25 else None)
            for i in range(40)]
        result = server.feed('score', limit=10, enrich=True)
        self.assertEqual(result['total'], 40)
        self.assertEqual(len(result['items']), 10)
        cached = next(iter(server.ranked_cache.values())).ranked
        self.assertEqual(len(cached.items), 30)
        self.assertEqual([call.args[0] for call in server.schedule.call_args_list],
                         [f'https://example.com/{i}' for i in range(25, 30)])



if __name__ == "__main__":
    unittest.main()


class LengthEnrichmentTests(FeedCacheTests):
    def setUp(self):
        super().setUp()
        ranking.TERMS["enrich_count"] = 5  # off by default

    def test_missing_subscription_length_is_scheduled_and_used(self):
        ranking.TERMS["length_per_minute"] = -2
        result = server.feed("score", enrich=True)
        server.schedule.assert_called_with(self.post["url"], force=True)
        self.assertEqual(result["items"][0]["score"], 0)
        server.cache[self.post["url"]] = {"estimated_minutes": 12, "length_version": 1}
        server.state_version += 1
        server.schedule.reset_mock()
        result = server.feed("score")
        self.assertEqual(result["items"][0]["score"], -24)
        server.schedule.assert_not_called()

    def test_missing_length_is_not_fetched_with_zero_weight(self):
        server.feed("score")
        server.schedule.assert_not_called()

    def test_feed_refresh_preserves_known_runtime(self):
        old = dict(self.post, estimated_minutes=20)
        merged = server.merge_posts([old], [dict(self.post, estimated_minutes=None)], 2000000)
        self.assertEqual(merged[0]["estimated_minutes"], 20)

    def test_playlist_enriches_existing_video_without_replacing_date(self):
        self.post["youtube"] = "abc"
        with patch.object(server.feeds, "older_videos", return_value=[
                dict(self.post, published=1, date_approx=True, estimated_minutes=15)]):
            server.load_older(self.url)
        post = server.subscriptions[self.url]["items"][0]
        self.assertEqual(post["estimated_minutes"], 15)
        self.assertEqual(post["published"], self.post["published"])

    def test_metadata_fetch_reads_youtube_player_runtime(self):
        import io
        from email.message import Message
        response = io.BytesIO(b'<html><script>{"lengthSeconds":"720"}</script></html>')
        response.headers = Message()
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.geturl = lambda: 'https://www.youtube.com/watch?v=abcdefghijk'
        with patch.object(server.feeds, "open_url", return_value=response):
            result = server.fetch_metadata(response.geturl())
        self.assertEqual(result['estimated_minutes'], 12)
        self.assertEqual(result['length_version'], 1)

    def test_video_update_gets_date_and_runtime_for_every_copy(self):
        self.post.update(youtube='abc', date_approx=True)
        duplicate = dict(self.post)
        server.subscriptions['other'] = {'items': [duplicate]}
        server.pending.add('video:abc')
        with patch.object(server.feeds, 'video_metadata', return_value={
                'published': 900000, 'estimated_minutes': 12}) as fetch:
            server.update_video_metadata('abc')
        fetch.assert_called_once_with('abc')
        for post in [self.post, duplicate]:
            self.assertEqual(post['published'], 900000)
            self.assertEqual(post['estimated_minutes'], 12)
            self.assertNotIn('date_approx', post)
        self.assertNotIn('video:abc', server.pending)

    def test_background_video_uses_existing_updater(self):
        self.post.update(youtube='abc', date_approx=True)
        ranking.TERMS['length_per_minute'] = -1
        with patch.object(server, 'schedule_video_metadata') as schedule:
            server.feed('score', enrich=True)
        schedule.assert_called_once_with('abc')
        server.schedule.assert_not_called()

    def test_opening_video_updates_missing_runtime_even_with_exact_date(self):
        self.post.update(youtube='abc')
        with patch.object(server, 'schedule_video_metadata') as schedule:
            self.assertTrue(server.set_viewed(self.post['url']))
        schedule.assert_called_once_with('abc')

    def test_failed_video_update_keeps_approximate_date_and_can_retry(self):
        self.post.update(youtube='abc', date_approx=True)
        with patch.object(server.feeds, 'video_metadata', return_value={}):
            server.update_video_metadata('abc')
        self.assertTrue(self.post['date_approx'])
        self.assertNotIn('video:abc', server.pending)
        with patch.object(server, 'schedule_video_metadata') as schedule:
            server.set_viewed(self.post['url'])
        schedule.assert_called_once_with('abc')


    def test_load_enriches_only_highest_five_incomplete_posts(self):
        posts = [dict(self.post, url=f'https://example.com/{i}', published=100 + i,
                      estimated_minutes=10 if i == 9 else None) for i in range(10)]
        server.subscriptions[self.url]['items'] = posts
        server.feed('score', enrich=True)
        self.assertEqual([call.args[0] for call in server.schedule.call_args_list],
                         [f'https://example.com/{i}' for i in (8, 7, 6, 5, 4)])
        server.schedule.reset_mock()
        server.feed('score')
        server.schedule.assert_not_called()

    def test_custom_count_and_zero_apply_on_cached_feed(self):
        server.feed('score')
        ranking.TERMS['enrich_count'] = 0
        server.feed('score', enrich=True)
        server.schedule.assert_not_called()
        ranking.TERMS['enrich_count'] = 1
        server.feed('score', enrich=True)
        server.schedule.assert_called_once_with(self.post['url'], force=True)

    def test_update_count_requires_integer_in_range(self):
        for invalid in [-1, 1.5, 101]:
            with self.assertRaises(ValueError):
                ranking.check_terms(dict(ranking.DEFAULT_TERMS, enrich_count=invalid))


class GitVersionTests(unittest.TestCase):
    @unittest.skipUnless((server.ROOT / ".git").exists(), "not running from a git checkout")
    def test_reads_a_checkout_owned_by_another_user_from_another_directory(self):
        # As the service: its own working directory, no home, and a checkout owned by root
        env = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent", "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
        with tempfile.TemporaryDirectory() as cwd, patch.dict(os.environ, env, clear=True), contextlib.chdir(cwd):
            self.assertRegex(server.git("rev-parse", "--short", "HEAD"), r"^[0-9a-f]{4,}$")
