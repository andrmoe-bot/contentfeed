import contextlib
import copy
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
        for name in ("LINKS_FILE", "FEEDS_FILE", "CACHE_FILE", "ADDED_FILE", "FEED_CACHE_FILE",
                     "VIEWED_FILE", "SKIPPED_FILE", "COLORS_FILE", "SETTINGS_FILE", "JELLYFIN_FILE"):
            self.stack.enter_context(patch.object(server, name, root / name, create=True))
        for name in ("subscriptions", "cache", "added", "viewed", "skipped", "colors", "ranked_cache"):
            self.stack.enter_context(patch.object(server, name, {}))
        self.stack.enter_context(patch.object(server, "pending", set()))
        self.stack.enter_context(patch.object(server, "state_version", 0))
        self.stack.enter_context(patch.object(ranking, "WEIGHTS", dict(ranking.DEFAULT_WEIGHTS)))
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
            self.assertIsNone(server.save_settings(dict(ranking.WEIGHTS, similar_penalty=0)))
            self.assertIsNone(server.feed("score")["items"][0]["similar"])
        self.assertEqual(rank.call_count, 3)

    def test_expired_cache_reranks(self):
        with patch.object(ranking, "rank", wraps=ranking.rank) as rank:
            server.feed("score")
            for key, (_, items) in list(server.ranked_cache.items()):
                server.ranked_cache[key] = (0, items)
            server.feed("score")
        self.assertEqual(rank.call_count, 2)

    def test_saved_settings_ignore_removed_tag_similarity(self):
        server.save_json(server.SETTINGS_FILE, {"weights": dict(ranking.DEFAULT_WEIGHTS, tag_similarity=99)})
        server.load_settings()
        self.assertEqual(ranking.WEIGHTS, ranking.DEFAULT_WEIGHTS)

    def test_state_changed_during_ranking_is_not_cached(self):
        original_rank = ranking.rank

        def rank_and_change(*args, **kwargs):
            result = original_rank(*args, **kwargs)
            server.save_json(server.COLORS_FILE, {})
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


if __name__ == "__main__":
    unittest.main()
