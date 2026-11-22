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

    def test_events_on_whole_days_are_all_day(self):
        data = calendar_data(
            "BEGIN:VEVENT\r\nUID:a\r\nDTSTART;VALUE=DATE:20261117\r\nDTEND;VALUE=DATE:20261118\r\nSUMMARY:Birthday\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:b\r\nDTSTART:20261117T120000Z\r\nDTEND:20261117T130000Z\r\nSUMMARY:Lunch\r\nEND:VEVENT",
            "BEGIN:VTODO\r\nUID:c\r\nDUE;VALUE=DATE:20261117\r\nSUMMARY:Task\r\nEND:VTODO")
        posts = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual({p["title"]: p["all_day"] for p in posts}, {"Birthday": True, "Lunch": False, "Task": False})

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

    def test_calendar_situations(self):
        busy = event("Meeting", NOW - 60, NOW + 1800)
        free = event("Work block", NOW - 600, NOW + 3600, free=True)
        later = event("Dinner", NOW + 7200, NOW + 9000)
        birthday = dict(event("Birthday", NOW - 3600, NOW + 80000), all_day=True)
        self.assertEqual(server.calendar_situations([busy, free, later, birthday], NOW),
                         {"busy": ("Meeting", NOW + 1800), "not_busy": ("Work block", NOW + 3600), "free_time": None})
        # All-day events don't count, so free time lasts until the next timed event starts.
        self.assertEqual(server.calendar_situations([later, birthday], NOW),
                         {"busy": None, "not_busy": None, "free_time": (None, NOW + 7200)})
        self.assertEqual(server.calendar_situations([], NOW)["free_time"], (None, None))

    def test_a_new_mode_is_off_until_a_situation_is_turned_on(self):
        self.assertIsNone(server.add_mode("Work"))
        self.assertEqual(server.modes[0]["when"], {"busy": False, "not_busy": False, "free_time": False})
        self.assertIsNone(server.choose_mode(None))
        now = time.time()
        self.set_events(event("work", now - 60, now + 3600, free=True, title="Work block"))
        self.assertEqual(server.feed("score")["mode"]["name"], None)
        self.assertIsNone(server.set_mode_when("work", {"not_busy": True}))
        self.assertEqual(server.feed("score")["mode"], {"name": "Work", "source": "calendar", "situation": "not_busy",
                                                        "event": "Work block", "until": now + 3600})
        self.assertEqual(server.set_mode_when("Work", {"later": True}), "there's no situation called later")
        self.assertEqual(server.set_mode_when("Work", {"busy": 1}), "each situation must be on (true) or off (false)")
        self.assertEqual(server.set_mode_when("Work", {}), "expected the situations to turn on or off")
        self.assertEqual(server.set_mode_when("Play", {"busy": True}), "there's no mode called Play")
        self.assertIsNone(server.set_mode_when("Work", {"not_busy": False}))
        self.assertEqual(server.feed("score")["mode"]["name"], None)

    def test_modes_turn_on_for_busy_events_free_events_or_free_time(self):
        for name in ("Busy", "Free", "Idle"):
            self.assertIsNone(server.add_mode(name))
        server.set_mode_when("Busy", {"busy": True})
        server.set_mode_when("Free", {"not_busy": True})
        server.set_mode_when("Idle", {"free_time": True})
        self.assertIsNone(server.choose_mode(None))
        now = time.time()
        mode = lambda: server.feed("score")["mode"]
        self.set_events(event("dinner", now + 600, now + 3600))
        self.assertEqual(mode(), {"name": "Idle", "source": "calendar", "situation": "free_time", "event": None,
                                  "until": now + 600})
        self.set_events(event("dinner", now - 60, now + 3600, title="Dinner"))
        self.assertEqual((mode()["name"], mode()["event"]), ("Busy", "Dinner"))
        self.set_events(event("work", now - 60, now + 3600, free=True))
        self.assertEqual(mode()["name"], "Free")
        # With both kinds taking place, the mode higher in the list wins.
        self.set_events(event("work", now - 60, now + 3600, free=True), event("call", now - 60, now + 600))
        self.assertEqual(mode()["name"], "Busy")
        self.assertIsNone(server.move_mode("free", 0))
        self.assertEqual([m["name"] for m in server.modes], ["Free", "Busy", "Idle"])
        self.assertEqual(mode()["name"], "Free")

    def test_moving_modes(self):
        for name in ("A", "B", "C"):
            self.assertIsNone(server.add_mode(name))
        self.assertIsNone(server.move_mode("A", 2))
        self.assertIsNone(server.move_mode("C", 0))
        self.assertEqual([m["name"] for m in server.modes], ["C", "B", "A"])
        self.assertEqual(json.loads(server.SETTINGS_FILE.read_text())["modes"][0]["name"], "C")
        for to in (3, -1, "1", True, None):
            self.assertEqual(server.move_mode("A", to), "the position must be a whole number from 0 to 2")
        self.assertEqual(server.move_mode("D", 0), "there's no mode called D")

    def test_the_starting_mode_is_normal_on_in_free_time_and_removable(self):
        server.modes[:] = server.starting_modes()
        self.assertEqual(server.settings()["modes"], [{"name": "Normal", "when": {"busy": False, "not_busy": False, "free_time": True},
                                                        "terms": ranking.DEFAULT_TERMS}])
        self.assertEqual(server.feed("score")["mode"]["name"], "Normal")
        self.assertIsNone(server.remove_mode("Normal"))
        self.assertEqual(server.modes, [])
        self.assertIsNone(server.add_mode("Normal"))  # an ordinary name

    def test_with_no_mode_on_the_feed_is_empty(self):
        result = server.feed("score")
        self.assertEqual((result["mode"], result["items"], result["post_limit"], result["total"]), ({"name": None, "source": "none", "situation": None, "event": None, "until": None}, [], 0, 1))
        # A subscription's page still lists its posts, ranked with the default settings.
        self.assertEqual(len(server.feed("score", subscription_url=self.feed_url)["items"]), 1)
        self.assertEqual(server.save_settings(dict(ranking.DEFAULT_TERMS)), "no mode is on")
        self.assertEqual(server.settings()["mode"], {"name": None, "source": "none", "situation": None, "event": None, "until": None})

    def test_each_mode_ranks_with_its_own_settings(self):
        self.assertIsNone(server.add_mode("Work"))
        self.assertIsNone(server.add_mode("Play"))
        work = dict(ranking.DEFAULT_TERMS, post_limit=0)
        self.assertIsNone(server.save_settings(work, "Work"))
        self.assertEqual(server.mode_terms("Play"), ranking.DEFAULT_TERMS)  # Play's settings are untouched
        self.assertIsNone(server.choose_mode("Work"))
        result = server.feed("score")
        self.assertEqual((result["mode"]["name"], result["items"], result["post_limit"]), ("Work", [], 0))
        self.assertIsNone(server.choose_mode("play"))
        self.assertEqual(len(server.feed("score")["items"]), 1)
        # Without a mode named, settings are saved for the mode in effect.
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, video_bonus=3)))
        self.assertEqual(server.mode_terms("Play")["video_bonus"], 3)
        self.assertEqual(server.save_settings(work, "Gone"), "there's no mode called Gone")

    def test_a_new_mode_copies_settings_and_a_chosen_mode_beats_the_calendar(self):
        self.assertIsNone(server.add_mode("Base"))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, video_bonus=5), "Base"))
        self.assertIsNone(server.add_mode(" Work ", "Base"))
        self.assertIsNone(server.add_mode("Fresh", "Missing"))  # a mode that doesn't exist: the defaults
        self.assertEqual(server.settings()["modes"][1], {"name": "Work", "when": dict.fromkeys(server.SITUATIONS, False),
                                                         "terms": dict(ranking.DEFAULT_TERMS, video_bonus=5)})
        self.assertEqual(server.mode_terms("Fresh"), ranking.DEFAULT_TERMS)
        self.assertEqual(server.settings()["mode"], {"name": "Fresh", "source": "chosen", "situation": None,
                                                     "event": None, "until": None})
        server.set_mode_when("Work", {"not_busy": True})
        now = time.time()
        self.set_events(event("work", now - 60, now + 3600, free=True, title="Work"))
        self.assertIsNone(server.choose_mode("Base"))
        self.assertEqual(server.settings()["mode"]["name"], "Base")
        self.assertIsNone(server.choose_mode(None))
        self.assertEqual(server.settings()["mode"]["name"], "Work")
        self.assertEqual(server.choose_mode("Play"), "there's no mode called Play")

    def test_mode_names_are_checked(self):
        self.assertIsNone(server.add_mode("Work"))
        for name, error in [("", "a mode needs a name"), ("work", "there's already a mode called work"),
                            ("Auto", "there's already a mode called Auto"),
                            ("x" * 41, "a mode's name can be at most 40 characters"), (5, "a mode needs a name"),
                            ("a\nb", "a mode's name can't have control characters")]:
            self.assertEqual(server.add_mode(name), error)

    def test_modes_and_the_choice_are_saved_and_loaded(self):
        self.assertIsNone(server.add_mode("Work"))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, task_bonus=100), "Work"))
        saved = json.loads(server.SETTINGS_FILE.read_text())
        self.assertEqual(saved["mode_choice"], "Work")
        self.assertEqual(saved["modes"][0]["terms"]["task_bonus"], 100)
        self.assertIsNone(server.set_mode_when("Work", {"free_time": True}))
        server.modes.clear()
        server.mode_choice.clear()
        server.load_settings()
        self.assertEqual(server.mode_terms("Work")["task_bonus"], 100)
        self.assertEqual(server.mode_choice, {"name": "Work"})
        self.assertEqual(server.find_mode("Work")["when"], {"busy": False, "not_busy": False, "free_time": True})
        # Removing the chosen mode follows the calendar again.
        self.assertIsNone(server.remove_mode("work"))
        self.assertEqual((server.modes, server.mode_choice), ([], {}))
        self.assertEqual(server.remove_mode("Work"), "there's no mode called Work")

    def test_saved_modes_get_new_terms_and_bad_ones_are_dropped(self):
        terms = {k: v for k, v in ranking.DEFAULT_TERMS.items() if k != "post_limit"}
        server.SETTINGS_FILE.write_text(json.dumps({"version": 2, "terms": ranking.DEFAULT_TERMS, "modes": [
            {"name": "Work", "terms": dict(terms, gone=1)}, {"name": "Bad", "terms": dict(terms, task_bonus="x")},
            {"name": "Never", "pattern": "", "terms": terms}, {"name": "Focus", "pattern": "^focus", "terms": terms},
            {"name": "Idle", "when": {"free_time": True, "busy": "yes", "gone": True}, "terms": terms},
            {"name": ""}, "junk"], "mode_choice": "Missing"}))
        server.load_settings()
        # Modes saved with a title pattern (or before patterns, matching their name) are on during free
        # events. The normal settings of before become the Normal mode, at the bottom.
        on = lambda m: [s for s in server.SITUATIONS if m["when"][s]]
        self.assertEqual([(m["name"], on(m)) for m in server.modes],
                         [("Work", ["not_busy"]), ("Never", []), ("Focus", ["not_busy"]), ("Idle", ["free_time"]),
                          ("Normal", ["free_time"])])
        self.assertEqual(server.mode_terms("Work"), ranking.DEFAULT_TERMS)
        self.assertEqual(server.mode_choice, {})

    def test_normal_settings_saved_before_modes_held_them_become_the_normal_mode(self):
        server.SETTINGS_FILE.write_text(json.dumps({"version": 2, "terms": dict(ranking.DEFAULT_TERMS, video_bonus=7),
                                                    "modes": [{"name": "Work", "when": {"busy": True}, "terms": ranking.DEFAULT_TERMS}],
                                                    "mode_choice": "Normal"}))
        server.load_settings()
        self.assertEqual([(m["name"], m["when"]) for m in server.modes],
                         [("Work", {"busy": True, "not_busy": False, "free_time": False}),
                          ("Normal", {"busy": False, "not_busy": False, "free_time": True})])
        self.assertEqual(server.mode_terms("Normal")["video_bonus"], 7)
        self.assertEqual(server.mode_choice, {"name": "Normal"})
        # Without saved settings, the modes are the starting ones; with the current version, as saved.
        server.SETTINGS_FILE.unlink()
        server.load_settings()
        self.assertEqual(server.modes, server.starting_modes())
        server.SETTINGS_FILE.write_text(json.dumps({"version": server.SETTINGS_VERSION, "modes": []}))
        server.load_settings()
        self.assertEqual(server.modes, [])

    def test_modes_api(self):
        body, code = server.modes_api({"add": "Work"})
        self.assertEqual((code, body["mode"]["name"], [m["name"] for m in body["modes"]]), (200, "Work", ["Work"]))
        body, code = server.modes_api({"choose": None})
        self.assertEqual((code, body["mode"]["source"]), (200, "none"))
        self.assertNotIn("terms", body)  # every mode's settings are in modes
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, white_points=0), "mode": "Work"})
        self.assertEqual((code, body["modes"][0]["terms"]["white_points"]), (200, 0))
        body, code = server.modes_api({"when": {"busy": True}, "mode": "Work"})
        self.assertEqual((code, body["modes"][0]["when"]), (200, {"busy": True, "not_busy": False, "free_time": False}))
        self.assertEqual(server.modes_api({"when": {"busy": "no"}, "mode": "Work"})[1], 400)
        server.add_mode("Play")
        body, code = server.modes_api({"move": "Play", "to": 0})
        self.assertEqual((code, [m["name"] for m in body["modes"]]), (200, ["Play", "Work"]))
        server.remove_mode("Play")
        body, code = server.modes_api({"remove": "Work"})
        self.assertEqual((code, body["modes"]), (200, []))
        self.assertEqual(server.modes_api({})[1], 400)


if __name__ == "__main__":
    unittest.main()
