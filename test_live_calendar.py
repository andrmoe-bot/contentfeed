import contextlib
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import caldav
import ranking
import server

CAL = "https://radicale.example/me/home/"


class CalendarVersionTests(unittest.TestCase):
    def response(self, props):
        return (f'<D:multistatus xmlns:D="DAV:" xmlns:CS="{caldav.CS}"><D:response><D:propstat><D:prop>{props}'
                '</D:prop></D:propstat></D:response></D:multistatus>').encode(), CAL, Message()

    def test_version_is_the_sync_token_or_else_the_ctag(self):
        for props, expected in [("<D:sync-token>http://radicale.org/ns/sync/abc</D:sync-token><CS:getctag>\"1\"</CS:getctag>",
                                 "http://radicale.org/ns/sync/abc"),
                                ("<CS:getctag>\"42\"</CS:getctag>", "\"42\""), ("", None)]:
            with self.subTest(props=props), patch.object(caldav, "_request", return_value=self.response(props)) as request:
                self.assertEqual(caldav.version(CAL), expected)
            self.assertEqual(request.call_args.args[1], "PROPFIND")
            self.assertEqual(request.call_args.kwargs, {"timeout": caldav.CHECK_TIMEOUT, "paced": False})


class LiveCalendarTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in server.DATA_FILES:
            self.stack.enter_context(patch.object(server, name, root / server.DATA_FILES[name]))
        for name in ("subscriptions", "cache", "added", "viewed", "skipped", "colors", "ranked_cache", "stop",
                     "calendar_checks"):
            self.stack.enter_context(patch.object(server, name, {}))
        self.stack.enter_context(patch.object(server, "pending", set()))
        self.stack.enter_context(patch.object(server, "state_version", 0))
        self.stack.enter_context(patch.object(ranking, "TERMS", dict(ranking.DEFAULT_TERMS)))
        self.stack.enter_context(patch.object(server, "schedule", return_value=None))
        self.stack.enter_context(patch.object(server, "schedule_subscription", return_value=None))
        self.stack.enter_context(patch.object(server.caldav, "read_logins", return_value=({}, None)))
        server.FEEDS_FILE.write_text(CAL + "\n")
        self.version = "v1"
        self.events = [self.dinner(10)]
        server.subscriptions[CAL] = {"title": "Home", "source": "caldav", "version": "v1", "items": [],
                                     "events": list(self.events), "fetched": time.time()}
        self.reads = 0
        self.stack.enter_context(patch.object(server.caldav, "version", side_effect=lambda url, login=None: self.current()))
        self.stack.enter_context(patch.object(server.caldav, "fetch", side_effect=self.read))

    def dinner(self, minutes, url="#dinner"):
        start = time.time() + minutes * 60
        return {"url": CAL + url, "title": "Dinner", "published": start, "calendar_kind": "event",
                "calendar_start": start, "deadline": start + 3600}

    def current(self):
        if isinstance(self.version, Exception):
            raise self.version
        return self.version

    def read(self, url, login=None):
        self.reads += 1
        return {"title": "Home", "site": CAL, "icon": None, "complete": True, "items": [],
                "events": list(self.events), "version": self.current()}

    def age_checks(self, seconds):
        for state in server.calendar_checks.values():
            state["checked"] -= seconds

    def test_unchanged_calendar_is_not_read_again(self):
        self.assertTrue(server.feed("score")["stopped"])
        self.assertEqual(self.reads, 0)

    def test_a_change_reaches_the_next_feed_request(self):
        self.assertTrue(server.feed("score")["stopped"])
        self.version, self.events = "v2", [self.dinner(120)]  # dinner moved two hours later
        self.age_checks(server.CALENDAR_CHECK_GAP)
        result = server.feed("score")
        self.assertEqual(self.reads, 1)
        self.assertFalse(result["stopped"])
        self.assertEqual(server.subscriptions[CAL]["version"], "v2")
        self.version, self.events = "v3", []  # deleted
        self.age_checks(server.CALENDAR_CHECK_GAP)
        self.assertIsNone(server.feed("score")["stop_at"])

    def test_ongoing_event_is_reported_with_its_calendar_and_details(self):
        self.assertIsNone(server.feed("score")["ongoing_event"])  # dinner starts in ten minutes
        dinner = dict(self.dinner(-10), description="At Kari's")
        server.subscriptions[CAL]["events"] = [dinner]
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["ongoing_event"], {
            "url": CAL + "#dinner", "title": "Dinner", "description": "At Kari's",
            "calendar_start": dinner["calendar_start"], "deadline": dinner["deadline"], "calendar": "Home"})

    def test_requests_within_the_gap_reuse_the_check(self):
        with patch.object(server.caldav, "version", return_value="v1") as version:
            server.feed("score")
            server.feed("score")
        self.assertEqual(version.call_count, 1)

    def test_unreachable_calendar_keeps_its_events_and_says_so(self):
        self.version = caldav.CalDAVError("couldn't fetch calendar: timed out")
        result = server.feed("score")
        self.assertTrue(result["stopped"])  # the events from the last read still stop the feed
        self.assertEqual([c["title"] for c in result["calendars_unreachable"]], ["Home"])
        with patch.object(server.caldav, "version", side_effect=AssertionError("checked too soon")):
            self.age_checks(server.CALENDAR_CHECK_GAP)
            server.feed("score")  # waits CALENDAR_RETRY before trying again
        self.version, self.events = "v2", []
        self.age_checks(server.CALENDAR_RETRY)
        result = server.feed("score")
        self.assertEqual(result["calendars_unreachable"], [])
        self.assertIsNone(result["stop_at"])

    def test_calendar_without_a_version_is_read_again_after_a_minute(self):
        self.version = None
        server.subscriptions[CAL]["version"] = None
        server.feed("score")
        self.assertEqual(self.reads, 0)
        server.subscriptions[CAL]["fetched"] -= server.CALENDAR_UNVERSIONED
        self.age_checks(server.CALENDAR_CHECK_GAP)
        server.feed("score")
        self.assertEqual(self.reads, 1)

    def test_shared_ics_links_are_left_to_the_timer(self):
        del server.subscriptions[CAL]["version"]  # an .ics read reports no version field
        with patch.object(server.caldav, "version", side_effect=AssertionError("not a collection")):
            server.feed("score")


if __name__ == "__main__":
    unittest.main()
