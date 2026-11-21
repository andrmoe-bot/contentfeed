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
        self.assertIsNone(ranking.minutes_past_stop(post, NOW, None))

    def test_minutes_past_stop_by_estimated_length(self):
        post = dict(item("post", "feed"), estimated_minutes=45)
        long_post = dict(item("long", "feed"), estimated_minutes=120)
        unknown = dict(item("unknown", "feed"))
        stop = NOW + 3600
        self.assertIsNone(ranking.minutes_past_stop(post, NOW, stop))  # 45 minutes fit in the hour
        self.assertIsNone(ranking.minutes_past_stop(dict(post, estimated_minutes=60), NOW, stop))  # exactly fits
        self.assertEqual(ranking.minutes_past_stop(post, NOW, NOW + 30 * 60), 15)
        self.assertEqual(ranking.minutes_past_stop(long_post, NOW, stop), 60)
        self.assertEqual(ranking.minutes_past_stop(dict(post, estimated_minutes=60.2), NOW, stop), 1)  # rounded up
        self.assertIsNone(ranking.minutes_past_stop(unknown, NOW, NOW + 1))  # unknown lengths get none

    def test_running_past_the_stop_costs_points_per_minute(self):
        post = dict(item("post", "feed"), estimated_minutes=45, past_stop=15)
        reasons = dict(ranking.score(post, NOW, ranking.DEFAULT_TERMS))
        self.assertEqual(reasons["runs 15 minutes past the stop time"], -300)
        off = dict(ranking.DEFAULT_TERMS, past_stop_per_minute=0)
        self.assertFalse(any("past the stop" in label for label, _ in ranking.score(post, NOW, off)))
        fits = dict(post, past_stop=None)
        self.assertFalse(any("past the stop" in label for label, _ in ranking.score(fits, NOW, ranking.DEFAULT_TERMS)))

    def test_events_act_as_stop_times_with_the_configured_lead(self):
        event = dict(item("event", "cal"), calendar_kind="event",
                     calendar_start=NOW + 2 * 3600, deadline=NOW + 3 * 3600)
        self.assertEqual(ranking.effective_stop([event], NOW, ranking.DEFAULT_TERMS),
                         (NOW + 2 * 3600 - 30 * 60, event))
        terms = dict(ranking.DEFAULT_TERMS, event_lead_minutes=0)
        self.assertEqual(ranking.effective_stop([event], NOW, terms), (NOW + 2 * 3600, event))
        terms = dict(ranking.DEFAULT_TERMS, event_lead_minutes=90)
        self.assertEqual(ranking.effective_stop([event], NOW, terms), (NOW + 2 * 3600 - 90 * 60, event))

    def test_ongoing_event_is_the_one_taking_place_that_ends_first(self):
        def event(name, start, end):
            return dict(item(name, "cal"), calendar_kind="event", calendar_start=start, deadline=end)
        upcoming = event("upcoming", NOW + 60, NOW + 3600)
        ended = event("ended", NOW - 3600, NOW)  # ends exactly now: over
        long = event("long", NOW - 7200, NOW + 7200)
        short = event("short", NOW - 60, NOW + 600)
        instant = dict(item("instant", "cal"), calendar_kind="event", calendar_start=NOW - 60)  # no end
        task = dict(event("task", NOW - 60, NOW + 60), calendar_kind="task")
        self.assertIsNone(ranking.ongoing_event([upcoming, ended, instant, task], NOW))
        self.assertIs(ranking.ongoing_event([long, upcoming, short], NOW), short)
        self.assertIs(ranking.ongoing_event([long, upcoming], NOW), long)

    def test_earliest_stop_wins(self):
        later = dict(item("later", "cal"), calendar_kind="event", calendar_start=NOW + 5 * 3600)
        sooner = dict(item("sooner", "cal"), calendar_kind="event", calendar_start=NOW + 3600)
        self.assertEqual(ranking.effective_stop([later, sooner], NOW, ranking.DEFAULT_TERMS),
                         (NOW + 3600 - 30 * 60, sooner))

    def test_ended_events_do_not_stop_watching_but_ongoing_ones_do(self):
        ended = dict(item("old", "cal"), calendar_kind="event", calendar_start=NOW - 7200, deadline=NOW - 3600)
        ongoing = dict(item("now", "cal"), calendar_kind="event", calendar_start=NOW - 600, deadline=NOW + 3600)
        self.assertEqual(ranking.effective_stop([ended], NOW, ranking.DEFAULT_TERMS), (None, None))
        # The ongoing event's lead time is already past, so its stop is in the past: stop now.
        self.assertEqual(ranking.effective_stop([ongoing], NOW, ranking.DEFAULT_TERMS),
                         (NOW - 600 - 30 * 60, ongoing))

    def test_calendar_items_never_run_past_the_stop(self):
        event = dict(item("event", "cal"), calendar_kind="event", calendar_start=NOW + 600, estimated_minutes=60)
        task = dict(item("task", "cal"), calendar_kind="task", deadline=NOW + 600, estimated_minutes=60)
        for it in (event, task):
            self.assertIsNone(ranking.minutes_past_stop(it, NOW, NOW + 1))

    def test_post_limit_is_a_whole_number_from_zero(self):
        self.assertEqual(ranking.DEFAULT_TERMS["post_limit"], ranking.LIMIT)  # no limit
        ranking.check_terms(dict(ranking.DEFAULT_TERMS, post_limit=0))
        for bad in (-1, 2.5):
            with self.assertRaises(ValueError):
                ranking.check_terms(dict(ranking.DEFAULT_TERMS, post_limit=bad))


class StoppedUntilTests(unittest.TestCase):
    """When the feed returns, and which event comes next, while watching is stopped."""
    def event(self, name, start, end=None, **extra):
        return dict(item(name, "cal"), title=name, calendar_kind="event", calendar_start=start, deadline=end, **extra)

    def test_not_stopped_means_nothing(self):
        later = self.event("later", NOW + 3 * 3600, NOW + 4 * 3600)
        self.assertEqual(ranking.stopped_until([later], NOW, ranking.DEFAULT_TERMS), (None, None))

    def test_an_ongoing_event_stops_until_it_ends(self):
        meeting = self.event("meeting", NOW - 600, NOW + 1800)
        self.assertEqual(ranking.stopped_until([meeting], NOW, ranking.DEFAULT_TERMS), (NOW + 1800, None))

    def test_in_the_lead_time_the_event_is_next(self):
        dinner = self.event("dinner", NOW + 600, NOW + 4200)
        self.assertEqual(ranking.stopped_until([dinner], NOW, ranking.DEFAULT_TERMS), (NOW + 4200, dinner))

    def test_events_whose_lead_time_starts_before_the_feed_returns_extend_the_stop(self):
        meeting = self.event("meeting", NOW - 600, NOW + 900)
        lunch = self.event("lunch", NOW + 900, NOW + 2700)  # its lead time begins during the meeting
        walk = self.event("walk", NOW + 2700 + 1700, NOW + 2700 + 3600)  # lead begins 100 s before lunch ends
        dinner = self.event("dinner", NOW + 4 * 3600, NOW + 5 * 3600)  # well after: the feed returns first
        until, upcoming = ranking.stopped_until([dinner, walk, lunch, meeting], NOW, ranking.DEFAULT_TERMS)
        self.assertEqual((until, upcoming), (NOW + 2700 + 3600, lunch))

    def test_an_event_without_an_end_stops_until_it_starts(self):
        reminder = self.event("reminder", NOW + 600)
        self.assertEqual(ranking.stopped_until([reminder], NOW, ranking.DEFAULT_TERMS), (NOW + 600, reminder))

    def test_free_and_all_day_events_never_stop_watching(self):
        free = self.event("free", NOW - 600, NOW + 1800, free=True)
        birthday = self.event("birthday", NOW - 3600, NOW + 20 * 3600, all_day=True)
        for events in ([free], [birthday]):
            self.assertEqual(ranking.effective_stop(events, NOW, ranking.DEFAULT_TERMS), (None, None))
            self.assertEqual(ranking.stopped_until(events, NOW, ranking.DEFAULT_TERMS), (None, None))
            self.assertIsNone(ranking.ongoing_event(events, NOW))


class StopServerTests(unittest.TestCase):
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

    def past_stop(self, result, url):
        """Whether the post got points for running past the stop time."""
        card = next(it for it in result["items"] if it["url"] == url)
        return any("past the stop" in r["label"] for r in card["reasons"])

    def order(self, result):
        return [it["url"] for it in result["items"]]

    def test_posts_that_would_run_past_the_stop_are_pushed_down_not_left_out(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # stop in 30 minutes
        server.subscriptions[self.url]["items"] = [self.long, self.short]
        self.long["published"] = 2_000_000  # newer: first without the stop
        result = server.feed("score")
        self.assertEqual(self.order(result), [self.short["url"], self.long["url"]])  # 10 minutes fit; 120 don't
        long_card = result["items"][1]
        self.assertTrue(any(r["label"] in ("runs 90 minutes past the stop time", "runs 91 minutes past the stop time")
                            for r in long_card["reasons"]))
        self.assertAlmostEqual(result["stop_at"], server.subscriptions[self.url]["events"][0]["calendar_start"] - 30 * 60, places=3)
        self.assertEqual(result["total"], 2)

    def test_viewed_post_gets_no_past_stop_points(self):
        """Reloading while you watch doesn't push down the post you're viewing."""
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # stop in 30 minutes
        server.viewed[self.long["url"]] = time.time()  # you're watching the long one now
        result = server.feed("score")
        long_card = next(it for it in result["items"] if it["url"] == self.long["url"])
        self.assertFalse(any("past the stop" in r["label"] for r in long_card["reasons"]))

    def test_only_the_latest_viewed_post_is_exempt(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # stop in 30 minutes
        other_long = {"url": "https://example.com/other-long", "title": "Other long", "first_seen": 1_000_000,
                      "published": 1_000_000, "estimated_minutes": 90}
        server.subscriptions[self.url]["items"].append(other_long)
        now = time.time()
        server.viewed[other_long["url"]] = now - 600  # opened earlier
        server.viewed[self.long["url"]] = now - 60  # opened last: this is the one you're viewing
        cards = {it["url"]: it for it in server.feed("score")["items"]}
        self.assertFalse(any("past the stop" in r["label"] for r in cards[self.long["url"]]["reasons"]))
        self.assertTrue(any("past the stop" in r["label"] for r in cards[other_long["url"]]["reasons"]))

    def test_post_limit_cuts_the_feed_but_not_a_subscription_page(self):
        server.save_settings(dict(ranking.DEFAULT_TERMS, post_limit=1))
        result = server.feed("score")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual((result["total"], result["post_limit"]), (2, 1))
        self.assertEqual(len(server.feed("score", subscription_url=self.url)["items"]), 2)
        server.save_settings(dict(ranking.DEFAULT_TERMS, post_limit=0))
        self.assertEqual(server.feed("score")["items"], [])  # an empty feed
        server.save_settings(dict(ranking.DEFAULT_TERMS))
        self.assertIsNone(server.feed("score")["post_limit"])  # no limit

    def test_fired_stop_keeps_the_feed_and_marks_it_stopped(self):
        """A fired stop doesn't empty the feed server-side: the response keeps the posts and sets
        stopped, and the page shows the stop screen in JS (feed hidden, playback force-stopped, alert)."""
        server.subscriptions[self.url]["events"] = [self.dinner(10)]  # within the lead time
        server.viewed[self.long["url"]] = time.time() - 60  # you're watching the long one
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual(self.urls(result), {self.short["url"], self.long["url"]})  # feed kept
        self.assertEqual(result["total"], 2)

    def test_fired_event_stop_keeps_the_feed_and_names_the_event(self):
        """Crossing an event's lead time marks the feed stopped and names the event, but keeps the
        feed itself — the page shows the stop screen in JS and back seamlessly."""
        start = time.time() + 10 * 60  # within the 30-minute lead: the time has run out
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        server.viewed[self.long["url"]] = time.time() - 60  # you're watching the long one
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual(self.urls(result), {self.short["url"], self.long["url"]})  # feed kept
        self.assertNotIn(event["url"], self.urls(result))  # events still aren't feed items
        self.assertEqual(result["stop_event"]["title"], "Dinner")  # the notice names it

    def test_event_stop_still_keeps_the_open_post_page(self):
        start = time.time() + 10 * 60  # within the 30-minute lead: the time has run out
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        result = server.feed("score", 300, post_url=self.long["url"])
        self.assertEqual(result["item"]["url"], self.long["url"])  # its page keeps the post
        self.assertTrue(result["stopped"])

    def test_stop_state_flip_rebuilds_the_cache_within_its_lifetime(self):
        """A stop-state flip (stop fired, or event ended) rebuilds the cached ranking even within
        the cache's lifetime, so the response's items and stop fields are always consistent."""
        t = time.time()
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": t + 30, "calendar_kind": "event", "calendar_start": t + 30,
                 "deadline": t + 45}  # stop = t+30-30min, fired at t; ends at t+45
        server.subscriptions[self.url]["events"] = [event]
        with patch.object(server.time, "time", lambda: t):
            fired = server.feed("score")
            self.assertTrue(fired["stopped"])
            self.assertEqual(self.urls(fired), {self.short["url"], self.long["url"]})  # feed kept
        with patch.object(server.time, "time", lambda: t + 50):  # event over; cache age 50s < TTL
            back = server.feed("score")
            self.assertFalse(back["stopped"])
            self.assertIsNone(back["stop_at"])
            self.assertIn(self.short["url"], self.urls(back))
            self.assertIn(self.long["url"], self.urls(back))

    def test_event_sets_the_stop_with_the_lead_time(self):
        start = time.time() + 2 * 3600
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        result = server.feed("score")
        self.assertAlmostEqual(result["stop_at"], start - 30 * 60, places=3)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["stop_event"]["title"], "Dinner")
        self.assertNotIn(event["url"], self.urls(result))  # events aren't feed items
        self.assertFalse(self.past_stop(result, self.short["url"]))
        self.assertTrue(self.past_stop(result, self.long["url"]))  # 120 minutes don't fit before the stop

    def test_events_are_not_feed_items_but_still_act_as_stop_times(self):
        start = time.time() + 5 * 3600  # far enough that both posts fit before the stop
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
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
        server.subscriptions[self.url]["events"] = [event]
        result = server.feed("score", 300, post_url=event["url"])
        self.assertEqual(result.get("error"), "Post not found")

    def test_lead_time_is_configurable_as_a_term(self):
        start = time.time() + 2 * 3600
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=90))
        result = server.feed("score")
        self.assertAlmostEqual(result["stop_at"], start - 90 * 60, places=3)

    def test_fired_event_stop_shows_no_content_but_names_the_event(self):
        start = time.time() + 10 * 60  # within the 30-minute lead: the time has run out
        event = {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                 "published": start, "calendar_kind": "event", "calendar_start": start,
                 "deadline": start + 3600}
        server.subscriptions[self.url]["events"] = [event]
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual(self.urls(result), {self.short["url"], self.long["url"]})  # feed kept
        self.assertEqual(result["stop_event"]["url"], event["url"])  # the notice names it instead
        self.assertAlmostEqual(result["stop_at"], start - 30 * 60, places=3)

    def test_post_page_keeps_its_post_even_when_it_no_longer_fits(self):
        server.subscriptions[self.url]["events"] = [self.dinner(60)]  # stop in 30 minutes
        result = server.feed("score", 300, post_url=self.long["url"])
        self.assertEqual(result["item"]["url"], self.long["url"])
        self.assertIsNotNone(result["stop_at"])

    def dinner(self, minutes_from_now):
        start = time.time() + minutes_from_now * 60
        return {"url": self.url + "#item=dinner", "title": "Dinner", "first_seen": 1_000_000,
                "published": start, "calendar_kind": "event", "calendar_start": start, "deadline": start + 3600}

    def test_stopped_response_says_when_the_feed_returns_and_what_is_next(self):
        meeting = dict(self.dinner(-10), url=self.url + "#item=meeting", title="Meeting")
        meeting["deadline"] = meeting["calendar_start"] + 20 * 60  # ends in 10 minutes
        lunch = dict(self.dinner(10), url=self.url + "#item=lunch", title="Lunch")  # right after
        server.subscriptions[self.url]["events"] = [meeting, lunch]
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual((result["ongoing_event"]["title"], result["next_event"]["title"]), ("Meeting", "Lunch"))
        self.assertAlmostEqual(result["stopped_until"], lunch["deadline"], places=3)
        server.subscriptions[self.url]["events"] = [self.dinner(120)]
        result = server.feed("score")
        self.assertEqual((result["stopped_until"], result["next_event"]), (None, None))

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

    def test_search_does_not_hide_an_event_stop(self):
        server.subscriptions[self.url]["events"] = [self.dinner(10)]
        result = server.feed("score", search="short")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stop_event"]["title"], "Dinner")

    def test_event_stop_applies_on_another_subscriptions_page(self):
        calendar = "https://calendar.example/home/"
        server.FEEDS_FILE.write_text(self.url + "\n" + calendar)
        server.subscriptions[calendar] = {"title": "Home", "items": [], "events": [self.dinner(10)], "fetched": 1}
        result = server.feed("score", 300, subscription_url=self.url)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["stop_event"]["title"], "Dinner")


if __name__ == "__main__":
    unittest.main()
