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


class StopFilterTests(unittest.TestCase):
    def test_lead_time_is_a_term_not_a_signed_coefficient(self):
        """The lead time is a duration setting, so it's not in SIGNED (coefficients are signed)."""
        self.assertEqual(ranking.DEFAULT_TERMS["event_lead_minutes"], 30)
        self.assertNotIn("event_lead_minutes", ranking.SIGNED)
        ranking.check_terms(ranking.DEFAULT_TERMS)
        ranking.check_terms(dict(ranking.DEFAULT_TERMS, event_lead_minutes=0))
        with self.assertRaises(ValueError):
            ranking.check_terms(dict(ranking.DEFAULT_TERMS, event_lead_minutes=-1))

    def test_no_stop_means_no_filter(self):
        post = dict(item("post", "feed"), estimated_minutes=60)
        self.assertEqual(ranking.effective_stop([post], NOW, ranking.DEFAULT_TERMS), (None, None))
        self.assertTrue(ranking.fits_before_stop(post, NOW, None))

    def test_timer_filters_by_estimated_length(self):
        post = dict(item("post", "feed"), estimated_minutes=45)
        long_post = dict(item("long", "feed"), estimated_minutes=120)
        unknown = dict(item("unknown", "feed"))
        stop = NOW + 3600
        self.assertEqual(ranking.effective_stop([post], NOW, ranking.DEFAULT_TERMS, stop), (stop, None))
        self.assertTrue(ranking.fits_before_stop(post, NOW, stop))  # 45 minutes fit in the hour
        self.assertFalse(ranking.fits_before_stop(post, NOW, NOW + 30 * 60))
        self.assertFalse(ranking.fits_before_stop(long_post, NOW, stop))
        self.assertTrue(ranking.fits_before_stop(unknown, NOW, NOW + 1))  # unknown lengths stay

    def test_past_timer_has_fired(self):
        """A stop time in the past is not ignored: it means the time has run out."""
        self.assertEqual(ranking.effective_stop([], NOW, ranking.DEFAULT_TERMS, NOW - 1), (NOW - 1, None))

    def test_events_act_as_stop_times_with_the_configured_lead(self):
        event = dict(item("event", "cal"), calendar_kind="event",
                     calendar_start=NOW + 2 * 3600, deadline=NOW + 3 * 3600)
        self.assertEqual(ranking.effective_stop([event], NOW, ranking.DEFAULT_TERMS),
                         (NOW + 2 * 3600 - 30 * 60, event))
        terms = dict(ranking.DEFAULT_TERMS, event_lead_minutes=0)
        self.assertEqual(ranking.effective_stop([event], NOW, terms), (NOW + 2 * 3600, event))
        terms = dict(ranking.DEFAULT_TERMS, event_lead_minutes=90)
        self.assertEqual(ranking.effective_stop([event], NOW, terms), (NOW + 2 * 3600 - 90 * 60, event))

    def test_earliest_stop_wins(self):
        event = dict(item("event", "cal"), calendar_kind="event", calendar_start=NOW + 5 * 3600)
        timer = NOW + 3600
        self.assertEqual(ranking.effective_stop([event], NOW, ranking.DEFAULT_TERMS, timer), (timer, None))
        timer = NOW + 10 * 3600
        self.assertEqual(ranking.effective_stop([event], NOW, ranking.DEFAULT_TERMS, timer),
                         (NOW + 5 * 3600 - 30 * 60, event))

    def test_ended_events_do_not_stop_watching_but_ongoing_ones_do(self):
        ended = dict(item("old", "cal"), calendar_kind="event", calendar_start=NOW - 7200, deadline=NOW - 3600)
        ongoing = dict(item("now", "cal"), calendar_kind="event", calendar_start=NOW - 600, deadline=NOW + 3600)
        self.assertEqual(ranking.effective_stop([ended], NOW, ranking.DEFAULT_TERMS), (None, None))
        # The ongoing event's lead time is already past, so its stop is in the past: stop now.
        self.assertEqual(ranking.effective_stop([ongoing], NOW, ranking.DEFAULT_TERMS),
                         (NOW - 600 - 30 * 60, ongoing))

    def test_calendar_items_are_never_filtered(self):
        event = dict(item("event", "cal"), calendar_kind="event", calendar_start=NOW + 600)
        task = dict(item("task", "cal"), calendar_kind="task", deadline=NOW + 600)
        for it in (event, task):
            self.assertTrue(ranking.fits_before_stop(it, NOW, NOW + 1))


class StopTimerServerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("LINKS_FILE", "FEEDS_FILE", "CACHE_FILE", "ADDED_FILE", "FEED_CACHE_FILE",
                     "VIEWED_FILE", "SKIPPED_FILE", "COLORS_FILE", "SETTINGS_FILE", "JELLYFIN_FILE",
                     "CALDAV_FILE", "STOP_FILE"):
            self.stack.enter_context(patch.object(server, name, root / name, create=True))
        for name in ("subscriptions", "cache", "added", "viewed", "skipped", "colors", "ranked_cache", "stop"):
            self.stack.enter_context(patch.object(server, name, {}))
        self.stack.enter_context(patch.object(server, "pending", set()))
        self.stack.enter_context(patch.object(server, "state_version", 0))
        self.stack.enter_context(patch.object(ranking, "TERMS", dict(ranking.DEFAULT_TERMS)))
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

    def test_set_stop_validates_persists_and_clears(self):
        self.assertEqual(server.set_stop("tomorrow"), "stop_at must be a unix time or null")
        self.assertEqual(server.set_stop(True), "stop_at must be a unix time or null")
        self.assertEqual(server.set_stop(-5), "stop_at must be a unix time or null")
        self.assertIsNone(server.set_stop(2_000_000))
        self.assertEqual(server.stop["stop_at"], 2_000_000.0)
        self.assertEqual(json.loads(server.STOP_FILE.read_text()), {"stop_at": 2_000_000.0})
        self.assertIsNone(server.set_stop(None))
        self.assertEqual(server.stop, {})
        self.assertEqual(json.loads(server.STOP_FILE.read_text()), {})

    def test_load_stop_ignores_invalid_and_stale_values(self):
        recent = time.time() - 3600
        server.STOP_FILE.write_text(json.dumps({"stop_at": recent}))
        server.load_stop()
        self.assertEqual(server.stop, {"stop_at": recent})
        server.stop.clear()
        # A timer more than a day in the past was never cleared: stale, not a fired stop.
        server.STOP_FILE.write_text(json.dumps({"stop_at": time.time() - 25 * 3600}))
        server.load_stop()
        self.assertEqual(server.stop, {})
        server.STOP_FILE.write_text(json.dumps({"stop_at": "soon"}))
        server.load_stop()
        self.assertEqual(server.stop, {})

    def test_settings_endpoint_reports_the_timer(self):
        self.assertIsNone(server.settings()["stop_at"])
        server.set_stop(2_000_000)
        self.assertEqual(server.settings()["stop_at"], 2_000_000.0)

    def test_feed_filters_posts_that_would_run_past_the_stop(self):
        server.set_stop(time.time() + 30 * 60)
        result = server.feed("score")
        self.assertIn(self.short["url"], self.urls(result))  # 10 minutes fit
        self.assertNotIn(self.long["url"], self.urls(result))  # 120 minutes don't
        self.assertEqual(result["stop_at"], server.stop["stop_at"])
        self.assertEqual(result["total"], 1)

    def test_viewed_post_stays_when_it_would_run_past_the_stop(self):
        """The post you're viewing doesn't move away as time passes: it stays even when it no longer fits."""
        server.set_stop(time.time() + 30 * 60)
        server.viewed[self.long["url"]] = time.time()  # you're watching the long one now
        result = server.feed("score")
        self.assertIn(self.short["url"], self.urls(result))  # 10 minutes fit
        self.assertIn(self.long["url"], self.urls(result))  # viewed, so kept although it doesn't fit
        self.assertEqual(result["total"], 2)

    def test_only_the_latest_viewed_post_is_kept(self):
        server.set_stop(time.time() + 30 * 60)
        other_long = {"url": "https://example.com/other-long", "title": "Other long", "first_seen": 1_000_000,
                      "published": 1_000_000, "estimated_minutes": 90}
        server.subscriptions[self.url]["items"].append(other_long)
        now = time.time()
        server.viewed[other_long["url"]] = now - 600  # opened earlier
        server.viewed[self.long["url"]] = now - 60  # opened last: this is the one you're viewing
        result = server.feed("score")
        self.assertIn(self.long["url"], self.urls(result))
        self.assertNotIn(other_long["url"], self.urls(result))  # only the latest viewed is exempt

    def test_skipped_post_is_not_kept(self):
        """A skip is not viewing: a skipped post that no longer fits is still left out."""
        server.set_stop(time.time() + 30 * 60)
        server.skipped[self.long["url"]] = time.time()
        result = server.feed("score")
        self.assertNotIn(self.long["url"], self.urls(result))
        self.assertEqual(result["total"], 1)

    def test_timer_stop_also_overrides_the_viewed_post(self):
        """A fired stop overrides the stay behavior whatever caused it: timer or event."""
        server.set_stop(time.time() - 10)  # the timer ran out
        server.viewed[self.long["url"]] = time.time() - 60  # you're watching the long one
        result = server.feed("score")
        self.assertEqual(self.urls(result), set())  # not even the viewed post stays
        self.assertTrue(result["stopped"])

    def test_fired_stop_still_keeps_the_open_post_page(self):
        server.set_stop(time.time() - 10)  # the timer ran out
        result = server.ranked_feed("score", 300, post_url=self.long["url"])
        self.assertEqual(result["item"]["url"], self.long["url"])  # its page keeps the post
        self.assertTrue(result["stopped"])

    def test_event_stop_overrides_the_viewed_post(self):
        """Crossing an event's lead time overrides the stay behavior: even the viewed post is left out."""
        start = time.time() + 10 * 60  # within the 30-minute lead: the time has run out
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        server.viewed[self.long["url"]] = time.time() - 60  # you're watching the long one
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual(self.urls(result), set())  # not even the viewed post stays
        self.assertEqual(result["stop_event"]["title"], "Dinner")

    def test_event_stop_still_keeps_the_open_post_page(self):
        start = time.time() + 10 * 60  # within the 30-minute lead: the time has run out
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        result = server.ranked_feed("score", 300, post_url=self.long["url"])
        self.assertEqual(result["item"]["url"], self.long["url"])  # its page keeps the post
        self.assertTrue(result["stopped"])

    def test_fired_timer_stops_the_feed_with_a_generic_message(self):
        server.set_stop(time.time() - 10)  # the time ran out
        result = server.feed("score")
        self.assertEqual(self.urls(result), set())  # no content shown anymore
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["stopped"])
        self.assertIsNone(result["stop_event"])
        self.assertEqual(result["stop_at"], server.stop["stop_at"])

    def test_clearing_the_timer_restores_the_feed(self):
        server.set_stop(time.time() + 30 * 60)
        self.assertNotIn(self.long["url"], self.urls(server.feed("score")))
        server.set_stop(None)
        result = server.feed("score")
        self.assertIn(self.long["url"], self.urls(result))
        self.assertIsNone(result["stop_at"])

    def test_event_sets_the_stop_with_the_lead_time(self):
        start = time.time() + 2 * 3600
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        result = server.feed("score")
        self.assertAlmostEqual(result["stop_at"], start - 30 * 60, places=3)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["stop_event"]["title"], "Dinner")
        self.assertNotIn(event["url"], self.urls(result))  # events aren't feed items
        self.assertIn(self.short["url"], self.urls(result))
        self.assertNotIn(self.long["url"], self.urls(result))  # 120 minutes don't fit before the stop

    def test_events_are_not_feed_items_but_still_act_as_stop_times(self):
        start = time.time() + 5 * 3600  # far enough that both posts fit before the stop
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        result = server.feed("score")
        self.assertNotIn(event["url"], self.urls(result))  # no event card
        self.assertIn(self.short["url"], self.urls(result))
        self.assertIn(self.long["url"], self.urls(result))
        self.assertAlmostEqual(result["stop_at"], start - 30 * 60, places=3)  # but it still stops watching
        self.assertEqual(result["stop_event"]["title"], "Dinner")

    def test_event_post_page_is_not_found(self):
        start = time.time() + 5 * 3600
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        result = server.ranked_feed("score", 300, post_url=event["url"])
        self.assertEqual(result.get("error"), "Post not found")

    def test_lead_time_is_configurable_as_a_term(self):
        start = time.time() + 2 * 3600
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=90))
        result = server.feed("score")
        self.assertAlmostEqual(result["stop_at"], start - 90 * 60, places=3)

    def test_fired_event_stop_shows_no_content_but_names_the_event(self):
        start = time.time() + 10 * 60  # within the 30-minute lead: the time has run out
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["items"].append(event)
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual(self.urls(result), set())  # no content, and no event card either
        self.assertEqual(result["stop_event"]["url"], event["url"])  # the notice names it instead
        self.assertAlmostEqual(result["stop_at"], start - 30 * 60, places=3)

    def test_post_page_keeps_its_post_even_when_it_no_longer_fits(self):
        server.set_stop(time.time() + 30 * 60)
        result = server.ranked_feed("score", 300, post_url=self.long["url"])
        self.assertEqual(result["item"]["url"], self.long["url"])
        self.assertEqual(result["stop_at"], server.stop["stop_at"])


if __name__ == "__main__":
    unittest.main()
