import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import icalendar_parser
import ranking
import server
from test_caldav import calendar_data
from test_ranking import NOW, item


def event(name, start, end, free=False, title=None):
    return dict(item(name, "cal"), title=title or name, calendar_kind="event", calendar_start=start,
                deadline=end, free=free)


class FreeEventTests(unittest.TestCase):
    def test_transparent_events_are_free_and_others_busy(self):
        data = calendar_data(
            "BEGIN:VEVENT\r\nUID:a\r\nDTSTART:20261117T080000Z\r\nDTEND:20261117T110000Z\r\n"
            "TRANSP:TRANSPARENT\r\nSUMMARY:Work block\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:b\r\nDTSTART:20261117T120000Z\r\nDTEND:20261117T130000Z\r\n"
            "TRANSP:OPAQUE\r\nSUMMARY:Lunch\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:c\r\nDTSTART:20261117T140000Z\r\nSUMMARY:Call\r\nEND:VEVENT",
            "BEGIN:VTODO\r\nUID:d\r\nSUMMARY:Task\r\nEND:VTODO")
        posts = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual({p["title"]: p["free"] for p in posts},
                         {"Work block": True, "Lunch": False, "Call": False, "Task": False})

    def test_free_events_neither_stop_watching_nor_count_as_ongoing(self):
        free = event("work", NOW - 60, NOW + 3600, free=True)
        later = event("later", NOW + 600, NOW + 3600, free=True)
        self.assertEqual(ranking.effective_stop([free, later], NOW, ranking.DEFAULT_TERMS), (None, None))
        self.assertIsNone(ranking.ongoing_event([free], NOW))
        busy = event("busy", NOW + 7200, NOW + 9000)
        self.assertEqual(ranking.effective_stop([free, busy], NOW, ranking.DEFAULT_TERMS)[1], busy)


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("LINKS_FILE", "FEEDS_FILE", "CACHE_FILE", "FEED_CACHE_FILE", "STATE_FILE",
                     "SETTINGS_FILE", "JELLYFIN_FILE", "CALDAV_FILE"):
            self.stack.enter_context(patch.object(server, name, root / name, create=True))
        for name in ("subscriptions", "cache", "added", "viewed", "colors", "ranked_cache", "mode_choice"):
            self.stack.enter_context(patch.object(server, name, {}))
        self.stack.enter_context(patch.object(server, "modes", []))
        self.stack.enter_context(patch.object(server, "pending", set()))
        self.stack.enter_context(patch.object(server, "state_version", 0))
        self.stack.enter_context(patch.object(ranking, "TERMS", dict(ranking.DEFAULT_TERMS)))
        self.stack.enter_context(patch.object(server, "schedule", return_value=None))
        self.stack.enter_context(patch.object(server, "schedule_subscription", return_value=None))
        self.feed_url, self.cal_url = "https://example.com/feed", "https://example.com/cal.ics"
        server.FEEDS_FILE.write_text(f"{self.feed_url}\n{self.cal_url}\n")
        post = {"url": "https://example.com/post", "title": "Post", "first_seen": 1_000_000, "published": 1_000_000}
        server.subscriptions[self.feed_url] = {"title": "Example", "feed_url": self.feed_url, "items": [post], "fetched": 1}
        server.subscriptions[self.cal_url] = {"title": "Calendar", "items": [], "events": [], "fetched": 1}

    def set_events(self, *events):
        server.subscriptions[self.cal_url]["events"] = list(events)

    def test_patterns_are_regular_expressions_ignoring_case(self):
        self.assertTrue(server.matches_pattern("Deep work", r"\bwork\b"))
        self.assertFalse(server.matches_pattern("Homework", r"\bwork\b"))
        self.assertTrue(server.matches_pattern("Focus: thesis", r"^(focus|deep work)\b"))
        self.assertFalse(server.matches_pattern("Anything", ""))  # empty: never on from the calendar

    def test_a_new_mode_matches_its_name_and_the_pattern_can_be_changed(self):
        self.assertIsNone(server.add_mode("C++"))
        self.assertEqual(server.modes[0]["pattern"], r"\bC\+\+\b")
        self.assertIsNone(server.add_mode("Work"))
        self.assertEqual(server.set_mode_pattern("work", r"^(work|focus)"), None)
        self.assertEqual(server.modes[1]["pattern"], r"^(work|focus)")
        self.assertIsNone(server.choose_mode(None))
        now = time.time()
        self.set_events(event("focus", now - 60, now + 3600, free=True, title="Focus time"))
        self.assertEqual(server.feed("score")["mode"], {"name": "Work", "source": "calendar", "event": "Focus time"})
        self.assertTrue(server.set_mode_pattern("Work", "(").startswith("the pattern isn't a valid regular expression"))
        self.assertEqual(server.set_mode_pattern("Work", 5), "the pattern must be text")
        self.assertEqual(server.set_mode_pattern("Work", "x" * 501), "the pattern can be at most 500 characters")
        self.assertEqual(server.set_mode_pattern("Play", "x"), "there's no mode called Play")
        self.assertIsNone(server.set_mode_pattern("Work", ""))
        self.assertEqual(server.feed("score")["mode"]["name"], "Normal")

    def test_a_free_event_naming_a_mode_turns_it_on_while_it_lasts(self):
        self.assertIsNone(server.add_mode("Work"))
        self.assertIsNone(server.choose_mode(None))
        now = time.time()
        self.set_events(event("work", now + 600, now + 3600, free=True, title="Work block"))
        self.assertEqual(server.feed("score")["mode"], {"name": "Normal", "source": "default", "event": None})
        self.set_events(event("work", now - 60, now + 3600, free=True, title="Work block"))
        self.assertEqual(server.feed("score")["mode"], {"name": "Work", "source": "calendar", "event": "Work block"})
        # A busy event with the name doesn't, as it stops watching instead.
        self.set_events(event("work", now - 60, now + 3600, title="Work block"))
        self.assertEqual(server.feed("score")["mode"]["name"], "Normal")

    def test_each_mode_ranks_with_its_own_settings(self):
        self.assertIsNone(server.add_mode("Work"))
        work = dict(ranking.DEFAULT_TERMS, post_limit=0)
        self.assertIsNone(server.save_settings(work, "Work"))
        result = server.feed("score")
        self.assertEqual((result["mode"]["name"], result["items"], result["post_limit"]), ("Work", [], 0))
        self.assertEqual(ranking.TERMS["post_limit"], ranking.LIMIT)  # Normal's settings are untouched
        self.assertIsNone(server.choose_mode("Normal"))
        self.assertEqual(len(server.feed("score")["items"]), 1)
        self.assertEqual(server.save_settings(work, "Play"), "there's no mode called Play")

    def test_a_new_mode_copies_settings_and_a_chosen_mode_beats_the_calendar(self):
        ranking.TERMS["video_bonus"] = 5
        self.assertIsNone(server.add_mode(" Work ", "Normal"))
        self.assertEqual(server.settings()["modes"], [{"name": "Work", "pattern": r"\bWork\b", "terms": dict(ranking.TERMS)}])
        self.assertEqual(server.settings()["mode"], {"name": "Work", "source": "chosen", "event": None})
        now = time.time()
        self.set_events(event("work", now - 60, now + 3600, free=True, title="Work"))
        self.assertIsNone(server.choose_mode("Normal"))
        self.assertEqual(server.settings()["mode"]["source"], "chosen")
        self.assertEqual(server.choose_mode("Play"), "there's no mode called Play")

    def test_mode_names_are_checked(self):
        self.assertIsNone(server.add_mode("Work"))
        for name, error in [("", "a mode needs a name"), ("work", "there's already a mode called work"),
                            ("normal", "there's already a mode called normal"), ("Auto", "there's already a mode called Auto"),
                            ("x" * 41, "a mode's name can be at most 40 characters"), (5, "a mode needs a name"),
                            ("a\nb", "a mode's name can't have control characters")]:
            self.assertEqual(server.add_mode(name), error)

    def test_modes_and_the_choice_are_saved_and_loaded(self):
        self.assertIsNone(server.add_mode("Work"))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, task_bonus=100), "Work"))
        saved = json.loads(server.SETTINGS_FILE.read_text())
        self.assertEqual(saved["mode_choice"], "Work")
        self.assertEqual(saved["modes"][0]["terms"]["task_bonus"], 100)
        server.modes.clear()
        server.mode_choice.clear()
        server.load_settings()
        self.assertEqual(server.mode_terms("Work")["task_bonus"], 100)
        self.assertEqual(server.mode_choice, {"name": "Work"})
        # Removing the chosen mode follows the calendar again.
        self.assertIsNone(server.remove_mode("work"))
        self.assertEqual((server.modes, server.mode_choice), ([], {}))
        self.assertEqual(server.remove_mode("Work"), "there's no mode called Work")

    def test_saved_modes_get_new_terms_and_bad_ones_are_dropped(self):
        terms = {k: v for k, v in ranking.DEFAULT_TERMS.items() if k != "post_limit"}
        server.SETTINGS_FILE.write_text(json.dumps({"version": 2, "terms": ranking.DEFAULT_TERMS, "modes": [
            {"name": "Work", "terms": dict(terms, gone=1)}, {"name": "Bad", "terms": dict(terms, task_bonus="x")},
            {"name": "Broken", "pattern": "(", "terms": terms}, {"name": "Focus", "pattern": "^focus", "terms": terms},
            {"name": ""}, "junk"], "mode_choice": "Missing"}))
        server.load_settings()
        self.assertEqual([(m["name"], m["pattern"]) for m in server.modes],
                         [("Work", r"\bWork\b"), ("Broken", ""), ("Focus", "^focus")])
        self.assertEqual(server.mode_terms("Work"), ranking.DEFAULT_TERMS)
        self.assertEqual(server.mode_choice, {})

    def test_modes_api(self):
        body, code = server.modes_api({"add": "Work"})
        self.assertEqual((code, body["mode"]["name"], [m["name"] for m in body["modes"]]), (200, "Work", ["Work"]))
        body, code = server.modes_api({"choose": None})
        self.assertEqual((code, body["mode"]["source"]), (200, "default"))
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, white_points=0), "mode": "Work"})
        self.assertEqual((code, body["modes"][0]["terms"]["white_points"]), (200, 0))
        body, code = server.modes_api({"pattern": "^deep", "mode": "Work"})
        self.assertEqual((code, body["modes"][0]["pattern"]), (200, "^deep"))
        self.assertEqual(server.modes_api({"pattern": "[", "mode": "Work"})[1], 400)
        body, code = server.modes_api({"remove": "Work"})
        self.assertEqual((code, body["modes"]), (200, []))
        self.assertEqual(server.modes_api({})[1], 400)


if __name__ == "__main__":
    unittest.main()
