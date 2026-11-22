import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ranking
import server
from test_ranking import NOW, item

# These tests use a 30-minute lead time (the default is 0).
LEAD_30 = dict(ranking.DEFAULT_TERMS, event_lead_minutes=30)


class ModeChangeTermTests(unittest.TestCase):
    def test_lead_time_is_a_term_not_a_signed_coefficient(self):
        """The lead time is a duration setting, so it's not in SIGNED (coefficients are signed)."""
        self.assertEqual(ranking.DEFAULT_TERMS["event_lead_minutes"], 0)
        self.assertNotIn("event_lead_minutes", ranking.SIGNED)
        ranking.check_terms(ranking.DEFAULT_TERMS)
        ranking.check_terms(dict(ranking.DEFAULT_TERMS, event_lead_minutes=0))
        with self.assertRaises(ValueError):
            ranking.check_terms(dict(ranking.DEFAULT_TERMS, event_lead_minutes=-1))

    def test_minutes_past_by_estimated_length(self):
        post = dict(item("post", "feed"), estimated_minutes=45)
        long_post = dict(item("long", "feed"), estimated_minutes=120)
        unknown = dict(item("unknown", "feed"))
        stop = NOW + 3600
        self.assertIsNone(ranking.minutes_past(post, NOW, stop))  # 45 minutes fit in the hour
        self.assertIsNone(ranking.minutes_past(dict(post, estimated_minutes=60), NOW, stop))  # exactly fits
        self.assertEqual(ranking.minutes_past(post, NOW, NOW + 30 * 60), 15)
        self.assertEqual(ranking.minutes_past(long_post, NOW, stop), 60)
        self.assertEqual(ranking.minutes_past(dict(post, estimated_minutes=60.2), NOW, stop), 1)  # rounded up
        self.assertIsNone(ranking.minutes_past(unknown, NOW, NOW + 1))  # unknown lengths get none
        self.assertIsNone(ranking.minutes_past(long_post, NOW, None))  # no mode change, nothing to run past

    def test_running_past_the_mode_change_costs_points_per_minute(self):
        post = dict(item("post", "feed"), estimated_minutes=45, past_mode_change=15)
        reasons = dict(ranking.score(post, NOW, LEAD_30))
        self.assertEqual(reasons["runs 15 minutes past the mode change"], -300)
        off = dict(LEAD_30, past_mode_change_per_minute=0)
        self.assertFalse(any("past the mode change" in label for label, _ in ranking.score(post, NOW, off)))
        fits = dict(post, past_mode_change=None)
        self.assertFalse(any("past the mode change" in label for label, _ in ranking.score(fits, NOW, LEAD_30)))

    def test_calendar_items_never_run_past_the_mode_change(self):
        event = dict(item("event", "cal"), calendar_kind="event", calendar_start=NOW + 600, estimated_minutes=60)
        task = dict(item("task", "cal"), calendar_kind="task", deadline=NOW + 600, estimated_minutes=60)
        for it in (event, task):
            self.assertIsNone(ranking.minutes_past(it, NOW, NOW + 1))

    def test_post_limit_is_a_whole_number_from_zero(self):
        self.assertEqual(ranking.DEFAULT_TERMS["post_limit"], ranking.LIMIT)  # no limit
        ranking.check_terms(dict(ranking.DEFAULT_TERMS, post_limit=0))
        for bad in (-1, 2.5):
            with self.assertRaises(ValueError):
                ranking.check_terms(dict(ranking.DEFAULT_TERMS, post_limit=bad))


class ModeChangeServerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("LINKS_FILE", "FEEDS_FILE", "CACHE_FILE", "FEED_CACHE_FILE",
                     "STATE_FILE", "SETTINGS_FILE", "JELLYFIN_FILE",
                     "CALDAV_FILE"):
            self.stack.enter_context(patch.object(server, name, root / name, create=True))
        for name in ("subscriptions", "cache", "added", "viewed", "colors", "ranked_cache"):
            self.stack.enter_context(patch.object(server, name, {}))
        self.stack.enter_context(patch.object(server, "pending", set()))
        self.stack.enter_context(patch.object(server, "state_version", 0))
        self.stack.enter_context(patch.object(ranking, "TERMS", dict(LEAD_30)))
        # Meeting, on during busy events, and Normal, on otherwise, both with the settings ranking.TERMS,
        # so the calendar changes the mode when a busy event begins (its start minus the lead time).
        self.stack.enter_context(patch.object(server, "modes", [
            {"name": "Meeting", "when": dict.fromkeys(server.SITUATIONS, False) | {"busy": True}, "terms": ranking.TERMS},
            {"name": "Normal", "when": dict.fromkeys(server.SITUATIONS, True) | {"busy": False}, "terms": ranking.TERMS}]))
        self.stack.enter_context(patch.object(server, "schedule", return_value=None))
        self.stack.enter_context(patch.object(server, "schedule_subscription", return_value=None))
        self.url = "https://example.com/feed"
        self.short = {"url": "https://example.com/short", "title": "Short", "first_seen": 1_000_000,
                      "published": 1_000_000, "estimated_minutes": 10}
        self.long = {"url": "https://example.com/long", "title": "Long", "first_seen": 1_000_000,
                     "published": 1_000_000, "estimated_minutes": 120}
        server.FEEDS_FILE.write_text(self.url)
        server.subscriptions[self.url] = {"title": "Example", "feed_url": self.url,
                                          "items": [self.short, self.long], "fetched": 1}

    def urls(self, result):
        return {it["url"] for it in result["items"]}

    def past_mode_change(self, result, url):
        """Whether the post got points for running past the next mode change."""
        card = next(it for it in result["items"] if it["url"] == url)
        return any("past the mode change" in r["label"] for r in card["reasons"])

    def order(self, result):
        return [it["url"] for it in result["items"]]

    def test_posts_that_would_run_past_the_mode_change_are_pushed_down_not_left_out(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # Meeting mode in 30 minutes
        server.subscriptions[self.url]["items"] = [self.long, self.short]
        self.long["published"] = 2_000_000  # newer: first without the mode change
        result = server.feed("score")
        self.assertEqual(self.order(result), [self.short["url"], self.long["url"]])  # 10 minutes fit; 120 don't
        long_card = result["items"][1]
        self.assertTrue(any(r["label"] in ("runs 90 minutes past the mode change", "runs 91 minutes past the mode change")
                            for r in long_card["reasons"]))
        self.assertAlmostEqual(result["mode"]["until"], server.subscriptions[self.url]["events"][0]["calendar_start"] - 30 * 60, places=3)
        self.assertEqual(result["total"], 2)

    def test_viewed_post_gets_no_past_mode_change_points(self):
        """Reloading while you watch doesn't push down the post you're viewing."""
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # Meeting mode in 30 minutes
        server.viewed[self.long["url"]] = time.time()  # you're watching the long one now
        result = server.feed("score")
        long_card = next(it for it in result["items"] if it["url"] == self.long["url"])
        self.assertFalse(any("past the mode change" in r["label"] for r in long_card["reasons"]))

    def test_only_the_latest_viewed_post_is_exempt(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # Meeting mode in 30 minutes
        other_long = {"url": "https://example.com/other-long", "title": "Other long", "first_seen": 1_000_000,
                      "published": 1_000_000, "estimated_minutes": 90}
        server.subscriptions[self.url]["items"].append(other_long)
        now = time.time()
        server.viewed[other_long["url"]] = now - 600  # opened earlier
        server.viewed[self.long["url"]] = now - 60  # opened last: this is the one you're viewing
        cards = {it["url"]: it for it in server.feed("score")["items"]}
        self.assertFalse(any("past the mode change" in r["label"] for r in cards[self.long["url"]]["reasons"]))
        self.assertTrue(any("past the mode change" in r["label"] for r in cards[other_long["url"]]["reasons"]))

    def test_a_free_event_changes_the_mode_like_a_busy_one(self):
        """A mode on during free events changes the mode when one begins, as for busy events."""
        server.modes.insert(0, {"name": "Work", "when": dict.fromkeys(server.SITUATIONS, False) | {"not_busy": True},
                                "terms": ranking.TERMS})
        work = dict(self.dinner(60), url=self.url + "#item=work", title="Work block", free=True)
        server.subscriptions[self.url]["events"] = [work]
        result = server.feed("score")
        self.assertAlmostEqual(result["mode"]["until"], work["calendar_start"] - 30 * 60, places=3)  # Work's lead time
        cards = {it["url"]: it for it in result["items"]}
        self.assertTrue(any(r["label"] in ("runs 90 minutes past the mode change", "runs 91 minutes past the mode change")
                            for r in cards[self.long["url"]]["reasons"]))
        self.assertFalse(self.past_mode_change(result, self.short["url"]))

    def test_the_points_count_from_the_next_mode_change(self):
        server.modes.insert(0, {"name": "Work", "when": dict.fromkeys(server.SITUATIONS, False) | {"not_busy": True},
                                "terms": dict(ranking.TERMS, event_lead_minutes=0)})
        work = dict(self.dinner(60), url=self.url + "#item=work", title="Work block", free=True)
        server.subscriptions[self.url]["events"] = [work, self.dinner(5 * 60)]
        result = server.feed("score")
        long_card = next(it for it in result["items"] if it["url"] == self.long["url"])
        self.assertTrue(any(r["label"] in ("runs 60 minutes past the mode change", "runs 61 minutes past the mode change")
                            for r in long_card["reasons"]))
        self.assertFalse(self.past_mode_change(result, self.short["url"]))

    def test_a_chosen_mode_does_not_change_so_gets_no_points(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]
        with patch.object(server, "mode_choice", {"name": "Normal"}):
            result = server.feed("score")
        self.assertIsNone(result["mode"]["until"])
        self.assertFalse(self.past_mode_change(result, self.long["url"]))

    def test_post_limit_cuts_the_feed_but_not_a_subscription_page(self):
        server.save_settings(dict(LEAD_30, post_limit=1))
        result = server.feed("score")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual((result["total"], result["post_limit"]), (2, 1))
        self.assertEqual(len(server.feed("score", subscription_url=self.url)["items"]), 2)
        server.save_settings(dict(LEAD_30, post_limit=0))
        self.assertEqual(server.feed("score")["items"], [])  # an empty feed
        server.save_settings(dict(LEAD_30))
        self.assertIsNone(server.feed("score")["post_limit"])  # no limit

    def test_events_are_not_feed_items(self):
        start = time.time() + 5 * 3600  # far enough that both posts fit before the mode changes
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        result = server.feed("score")
        self.assertNotIn(event["url"], self.urls(result))  # no event card
        self.assertIn(self.short["url"], self.urls(result))
        self.assertIn(self.long["url"], self.urls(result))
        self.assertAlmostEqual(result["mode"]["until"], start - 30 * 60, places=3)  # but it turns Meeting on

    def test_event_post_page_is_not_found(self):
        start = time.time() + 5 * 3600
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        result = server.feed("score", 300, post_url=event["url"])
        self.assertEqual(result.get("error"), "Post not found")

    def test_post_page_keeps_its_post_even_when_it_no_longer_fits(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # Meeting mode in 30 minutes
        result = server.feed("score", 300, post_url=self.long["url"])
        self.assertEqual(result["item"]["url"], self.long["url"])

    def dinner(self, minutes_from_now):
        start = time.time() + minutes_from_now * 60
        return {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                "published": start, "calendar_kind": "event", "calendar_start": start, "deadline": start + 3600}

    def test_a_busy_event_stops_watching_only_through_a_mode(self):
        """Busy events have no stop of their own: a mode on during them with a post limit of 0, from
        its lead time, empties the feed, and without one the feed carries on."""
        server.subscriptions[self.url]["events"] = [self.dinner(10)]  # within Meeting's 30-minute lead
        result = server.feed("score")
        self.assertEqual(result["mode"]["name"], "Meeting")
        self.assertEqual(len(result["items"]), 2)
        for key in ("stop_at", "stopped", "stop_event", "ongoing_event", "stopped_until", "next_event"):
            self.assertNotIn(key, result)
        server.modes[0]["terms"] = dict(ranking.TERMS)  # Meeting's own, not shared with Normal
        server.save_settings(dict(LEAD_30, post_limit=0), "Meeting")
        result = server.feed("score")
        self.assertEqual((result["items"], result["post_limit"]), ([], 0))
        self.assertEqual(len(server.feed("score", subscription_url=self.url)["items"]), 2)  # its page still lists them
        server.subscriptions[self.url]["events"] = [self.dinner(45)]  # before the lead time
        self.assertEqual((server.feed("score")["mode"]["name"], len(server.feed("score")["items"])), ("Normal", 2))

    def test_events_saved_among_posts_move_to_events_at_startup(self):
        event = self.dinner(120)
        server.subscriptions[self.url]["items"].append(event)
        self.assertTrue(server.migrate_calendar_events())
        self.assertEqual(server.subscriptions[self.url]["events"], [event])
        self.assertNotIn(event, server.subscriptions[self.url]["items"])
        self.assertFalse(server.migrate_calendar_events())  # nothing left to move

    def test_events_are_not_counted_as_posts(self):
        server.subscriptions[self.url]["events"] = [self.dinner(120)]
        status = server.subscription_status()[0]
        self.assertEqual(status["posts"], 2)
        self.assertEqual(status["unseen"], 2)


if __name__ == "__main__":
    unittest.main()
