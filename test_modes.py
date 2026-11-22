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

OWN, GLOBAL = ranking.split_terms(ranking.DEFAULT_TERMS)  # a mode's own default settings, and the global ones


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


class ServerTestCase(unittest.TestCase):
    """A server with one subscription, one calendar and no modes."""

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
        self.stack.enter_context(patch.object(server, "global_terms", ranking.split_terms(ranking.DEFAULT_TERMS)[1]))
        self.stack.enter_context(patch.object(server, "schedule", return_value=None))
        self.stack.enter_context(patch.object(server, "schedule_subscription", return_value=None))
        self.feed_url, self.cal_url = "https://example.com/feed", "https://example.com/cal.ics"
        server.FEEDS_FILE.write_text(f"{self.feed_url}\n{self.cal_url}\n")
        post = {"url": "https://example.com/post", "title": "Post", "first_seen": 1_000_000, "published": 1_000_000}
        server.subscriptions[self.feed_url] = {"title": "Example", "feed_url": self.feed_url, "items": [post], "fetched": 1}
        server.subscriptions[self.cal_url] = {"title": "Calendar", "items": [], "events": [], "fetched": 1}

    def set_events(self, *events):
        server.subscriptions[self.cal_url]["events"] = list(events)


class ModeTests(ServerTestCase):
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
                                                        "event": "Work block", "until": now + 3600, "next": None})
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
        for name in ("Busy", "Free"):  # events turn their modes on when they start (see LeadTimeTests)
            server.find_mode(name)["terms"]["event_lead_minutes"] = 0
        self.assertIsNone(server.choose_mode(None))
        now = time.time()
        mode = lambda: server.feed("score")["mode"]
        self.set_events(event("dinner", now + 600, now + 3600))
        self.assertEqual(mode(), {"name": "Idle", "source": "calendar", "situation": "free_time", "event": None,
                                  "until": now + 600, "next": "Busy"})
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
                                                        "terms": OWN}])
        self.assertEqual(server.feed("score")["mode"]["name"], "Normal")
        self.assertIsNone(server.remove_mode("Normal"))
        self.assertEqual(server.modes, [])
        self.assertIsNone(server.add_mode("Normal"))  # an ordinary name

    def test_with_no_mode_on_the_feed_is_empty(self):
        result = server.feed("score")
        self.assertEqual((result["mode"], result["items"], result["post_limit"], result["total"]), ({"name": None, "source": "none", "situation": None, "event": None, "until": None, "next": None}, [], 0, 1))
        # A subscription's page still lists its posts, ranked with the default settings.
        self.assertEqual(len(server.feed("score", subscription_url=self.feed_url)["items"]), 1)
        self.assertEqual(server.settings()["mode"], {"name": None, "source": "none", "situation": None, "event": None, "until": None, "next": None})

    def test_each_mode_ranks_with_its_own_settings(self):
        self.assertIsNone(server.add_mode("Work"))
        self.assertIsNone(server.add_mode("Play"))
        work = dict(ranking.DEFAULT_TERMS, post_limit=0)
        self.assertIsNone(server.save_settings(work, "Work"))
        self.assertEqual(server.mode_terms("Play"), OWN)  # Play's settings are untouched
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
                                                         "terms": dict(OWN, video_bonus=5)})
        self.assertEqual(server.mode_terms("Fresh"), OWN)
        self.assertEqual(server.settings()["mode"], {"name": "Fresh", "source": "chosen", "situation": None,
                                                     "event": None, "until": None, "next": None})
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
        self.assertEqual(server.mode_terms("Work"), OWN)
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



class GlobalTermsTests(ServerTestCase):
    """Fetch details is the same in every mode, and applies with none on."""

    def setUp(self):
        super().setUp()
        for name in ("Work", "Play"):
            self.assertIsNone(server.add_mode(name))
        self.assertIsNone(server.choose_mode("Work"))

    def save(self, mode=None, **changes):
        return server.save_settings(dict(ranking.DEFAULT_TERMS, **changes), mode)

    def test_the_only_global_setting_is_fetch_details(self):
        self.assertEqual(ranking.GLOBAL_TERMS, {"enrich_count"})
        self.assertEqual((set(OWN) | set(GLOBAL), set(OWN) & set(GLOBAL)), (set(ranking.DEFAULT_TERMS), set()))
        self.assertIn("event_lead_minutes", OWN)
        data = server.settings()
        self.assertEqual(data["global"], {"enrich_count": 0})
        for mode in data["modes"]:
            self.assertEqual(mode["terms"], OWN)  # modes don't have it

    def test_saving_in_one_mode_changes_it_for_every_mode(self):
        self.assertIsNone(self.save("Work", enrich_count=3, video_bonus=4))
        self.assertEqual(server.global_terms, {"enrich_count": 3})
        self.assertEqual(server.mode_terms("Work")["video_bonus"], 4)
        self.assertEqual(server.mode_terms("Play"), OWN)  # the mode's own settings stay its own
        for name in ("Work", "Play", None):
            self.assertEqual(server.terms_for(name)["enrich_count"], 3, name)
        self.assertIsNone(self.save("Play", enrich_count=7))
        self.assertEqual(server.terms_for("Work")["enrich_count"], 7)
        self.assertEqual(server.terms_for("Work")["video_bonus"], 4)

    def test_fetch_details_applies_whatever_the_mode(self):
        self.assertIsNone(self.save("Play", enrich_count=4))
        for choice in ("Work", "Play", None):
            server.choose_mode(choice)
            with patch.object(server, "enrich_posts") as enrich:
                server.feed("score", enrich=True, subscription_url=self.feed_url)
            self.assertEqual(enrich.call_args.args[1], 4, choice)

    def test_with_no_mode_on_only_the_global_settings_are_saved(self):
        self.assertIsNone(server.choose_mode(None))
        self.assertIsNone(server.feed("score")["mode"]["name"])
        before = [dict(m["terms"]) for m in server.modes]
        self.assertIsNone(self.save(enrich_count=5, video_bonus=99, event_lead_minutes=1))
        self.assertEqual(server.global_terms, {"enrich_count": 5})
        self.assertEqual([m["terms"] for m in server.modes], before)
        self.assertEqual(json.loads(server.SETTINGS_FILE.read_text())["global"], {"enrich_count": 5})

    def test_invalid_settings_change_nothing(self):
        self.assertIsNone(self.save("Work", enrich_count=2))
        for bad in ({"enrich_count": -1}, {"enrich_count": 1.5}, {"enrich_count": 101}, {"video_bonus": "x"},
                    {"event_lead_minutes": -1}):
            self.assertIsNotNone(self.save("Work", **{"enrich_count": 2, **bad}), bad)
            self.assertEqual(server.global_terms, {"enrich_count": 2})
            self.assertEqual(server.mode_terms("Work"), OWN)
        missing = {k: v for k, v in ranking.DEFAULT_TERMS.items() if k != "enrich_count"}
        self.assertIsNotNone(server.save_settings(missing, "Work"))
        self.assertEqual(self.save("Gone", enrich_count=9), "there's no mode called Gone")
        self.assertEqual(server.global_terms["enrich_count"], 2)  # not saved for a mode that isn't there

    def test_new_and_removed_modes_leave_it_alone(self):
        self.assertIsNone(self.save("Work", enrich_count=2))
        self.assertIsNone(server.add_mode("Copy", "Work"))
        self.assertNotIn("enrich_count", server.find_mode("Copy")["terms"])
        for name in ("Work", "Play", "Copy"):
            self.assertIsNone(server.remove_mode(name))
        self.assertEqual(server.global_terms, {"enrich_count": 2})
        self.assertIsNone(server.add_mode("Again"))
        self.assertEqual(server.terms_for("Again")["enrich_count"], 2)

    def test_it_is_saved_once_and_loaded(self):
        self.assertIsNone(self.save("Work", enrich_count=6, video_bonus=2, event_lead_minutes=10))
        saved = json.loads(server.SETTINGS_FILE.read_text())
        self.assertEqual(saved["version"], server.SETTINGS_VERSION)
        self.assertEqual(saved["global"], {"enrich_count": 6})
        for mode in saved["modes"]:
            self.assertNotIn("enrich_count", mode["terms"])
        server.global_terms.update(GLOBAL)
        server.modes.clear()
        server.load_settings()
        self.assertEqual(server.global_terms, {"enrich_count": 6})
        self.assertEqual(server.mode_terms("Work"), dict(OWN, video_bonus=2, event_lead_minutes=10))
        self.assertEqual(server.terms_for("Play")["enrich_count"], 6)

    def load(self, data):
        server.SETTINGS_FILE.write_text(json.dumps(data))
        server.load_settings()
        return dict(server.global_terms)

    def test_settings_saved_before_global_ones_take_it_from_normal_else_the_first_mode(self):
        mode = lambda name, **terms: {"name": name, "when": {"free_time": True}, "terms": dict(ranking.DEFAULT_TERMS, **terms)}
        # Version 3: from the Normal mode, wherever it is in the list; each mode keeps its own lead time.
        self.assertEqual(self.load({"version": 3, "modes": [mode("Work", enrich_count=1, event_lead_minutes=5),
                                                            mode("Normal", enrich_count=7, event_lead_minutes=50)]}),
                         {"enrich_count": 7})
        self.assertEqual([m["terms"] for m in server.modes], [dict(OWN, event_lead_minutes=5), dict(OWN, event_lead_minutes=50)])
        # Without a Normal mode, from the first; without modes, the default.
        self.assertEqual(self.load({"version": 3, "modes": [mode("Work", enrich_count=4), mode("Play", enrich_count=9)]}),
                         {"enrich_count": 4})
        self.assertEqual(self.load({"version": 3, "modes": []}), GLOBAL)
        # Before version 3, from the normal settings, which become the Normal mode, with their lead time.
        self.assertEqual(self.load({"version": 2, "terms": dict(ranking.DEFAULT_TERMS, event_lead_minutes=40, enrich_count=2),
                                    "modes": [mode("Work", enrich_count=5)]}), {"enrich_count": 2})
        self.assertEqual(server.mode_terms("Normal")["event_lead_minutes"], 40)
        self.assertEqual(self.load({"terms": dict(ranking.DEFAULT_TERMS, enrich_count=12)}), {"enrich_count": 12})
        # Without saved settings, the default.
        server.SETTINGS_FILE.unlink()
        server.global_terms.update(enrich_count=99)
        server.load_settings()
        self.assertEqual(server.global_terms, GLOBAL)

    def test_the_past_stop_points_become_the_past_mode_change_points(self):
        old = {k: v for k, v in ranking.DEFAULT_TERMS.items() if k != "past_mode_change_per_minute"}
        mode = {"name": "Work", "when": {"busy": True}, "terms": dict(old, past_stop_per_minute=-7)}
        self.load({"version": 4, "global": {}, "modes": [mode]})
        self.assertEqual(server.mode_terms("Work")["past_mode_change_per_minute"], -7)
        self.assertNotIn("past_stop_per_minute", server.mode_terms("Work"))
        self.load({"version": 2, "terms": dict(old, past_stop_per_minute=-3), "modes": []})
        self.assertEqual(server.mode_terms("Normal")["past_mode_change_per_minute"], -3)

    def test_bad_or_partial_saved_global_settings(self):
        modes = [{"name": "Work", "when": {}, "terms": dict(ranking.DEFAULT_TERMS, event_lead_minutes=15)}]
        # Unknown settings and mode settings there are dropped: the lead time stays the mode's.
        self.assertEqual(self.load({"version": 4, "global": {"enrich_count": 3, "event_lead_minutes": 99, "video_bonus": 5,
                                                             "gone": 1}, "modes": modes}), {"enrich_count": 3})
        self.assertEqual((server.terms_for("Work")["video_bonus"], server.terms_for("Work")["event_lead_minutes"]), (0, 15))
        self.assertEqual(self.load({"version": 4, "global": {}, "modes": modes}), GLOBAL)  # missing: the default
        with patch("builtins.print") as said:
            self.assertEqual(self.load({"version": 4, "global": {"enrich_count": -5}, "modes": modes}), GLOBAL)
        self.assertIn("Ignoring the global settings", said.call_args.args[0])
        self.assertEqual(self.load({"version": 4, "global": "junk", "modes": modes}), GLOBAL)  # from Work, the first mode

    def test_the_api_serves_and_saves_it(self):
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, enrich_count=8), "mode": "Play"})
        self.assertEqual((code, body["global"]), (200, {"enrich_count": 8}))
        self.assertTrue(all("enrich_count" not in m["terms"] for m in body["modes"]))
        body, code = server.modes_api({"choose": None})
        self.assertEqual((code, body["mode"]["name"], body["global"]["enrich_count"]), (200, None, 8))
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, enrich_count=1)})
        self.assertEqual((code, body["global"]["enrich_count"]), (200, 1))
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, enrich_count=-1)})
        self.assertEqual((code, body["global"]["enrich_count"]), (400, 1))


class LeadTimeTests(ServerTestCase):
    """A mode's lead time: the mode turns on that long before an event it's on for, and a busy event
    stops watching then too."""

    def setUp(self):
        super().setUp()
        self.now = time.time()

    def mode(self, name, situations, lead):
        """Add a mode on in the situations, with the lead time in minutes, and follow the calendar."""
        self.assertIsNone(server.add_mode(name))
        if situations:
            self.assertIsNone(server.set_mode_when(name, dict.fromkeys(situations, True)))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=lead), name))
        self.assertIsNone(server.choose_mode(None))

    def at(self, minutes):
        return self.now + minutes * 60

    def follow(self, minutes=0):
        """The mode the calendar has on, minutes from now, as (name, until, next)."""
        server.choose_mode(None)
        mode = server.active_mode(server.calendar_events(list(server.subscriptions)), self.at(minutes))
        return mode["name"], mode["until"], mode["next"]

    def test_the_lead_time_is_each_modes_own(self):
        self.mode("Meeting", {"busy"}, 30)
        self.mode("Focus", {"not_busy"}, 5)
        self.assertEqual((server.mode_terms("Meeting")["event_lead_minutes"], server.mode_terms("Focus")["event_lead_minutes"]), (30, 5))
        self.assertNotIn("event_lead_minutes", server.global_terms)
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=45), "Focus"))
        self.assertEqual(server.mode_terms("Meeting")["event_lead_minutes"], 30)

    def test_a_meeting_mode_turns_on_its_lead_time_before_the_meeting(self):
        self.mode("Meeting", {"busy"}, 30)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("standup", self.at(60), self.at(90), title="Standup"))
        self.assertEqual(self.follow(), ("Normal", self.at(30), "Meeting"))
        self.assertEqual(self.follow(29), ("Normal", self.at(30), "Meeting"))
        self.assertEqual(self.follow(30), ("Meeting", self.at(90), "Normal"))  # ready when it starts
        self.assertEqual(server.active_mode(server.calendar_events(list(server.subscriptions)), self.at(30))["event"], "Standup")
        self.assertEqual(self.follow(90), ("Normal", None, None))

    def test_watching_stops_when_the_mode_begins(self):
        self.mode("Meeting", {"busy"}, 30)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("standup", self.at(60), self.at(90)))
        result = server.feed("score")
        self.assertEqual((result["mode"]["name"], result["stop_at"]), ("Normal", self.at(30)))
        self.assertEqual(result["mode"]["until"], result["stop_at"])
        # It's the lead time of the mode the meeting turns on, whatever mode is in effect.
        server.choose_mode("Normal")
        self.assertEqual(server.feed("score")["stop_at"], self.at(30))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=10), "Meeting"))
        self.assertEqual(server.feed("score")["stop_at"], self.at(50))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=120), "Normal"))
        self.assertEqual(server.feed("score")["stop_at"], self.at(50))  # Normal isn't on during busy events

    def test_while_stopped_in_the_lead_time_the_meeting_is_up_next(self):
        self.mode("Meeting", {"busy"}, 30)
        self.set_events(event("standup", self.at(20), self.at(50), title="Standup"))
        result = server.feed("score")
        self.assertTrue(result["stopped"])
        self.assertEqual((result["stopped_until"], result["next_event"]["title"], result["ongoing_event"]),
                         (self.at(50), "Standup", None))
        self.assertEqual(result["stop_event"]["title"], "Standup")
        self.assertNotIn("begins", result["stop_event"])  # the event as it is, not the copy with its lead

    def test_an_event_with_no_mode_for_it_stops_watching_at_its_start(self):
        self.mode("Normal", {"free_time"}, 30)  # free time doesn't use the lead time
        self.set_events(event("dentist", self.at(60), self.at(120)))
        self.assertEqual(server.feed("score")["stop_at"], self.at(60))
        self.assertEqual(self.follow(), ("Normal", self.at(60), None))

    def test_the_highest_mode_on_for_the_event_sets_the_lead_time(self):
        self.mode("Meeting", {"busy"}, 15)
        self.mode("Calls", {"busy"}, 45)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("call", self.at(60), self.at(90)))
        self.assertEqual(server.feed("score")["stop_at"], self.at(45))
        self.assertEqual(self.follow(), ("Normal", self.at(45), "Meeting"))
        server.move_mode("Calls", 0)
        self.assertEqual(server.feed("score")["stop_at"], self.at(15))
        self.assertEqual(self.follow(), ("Normal", self.at(15), "Calls"))

    def test_free_events_turn_their_mode_on_early_but_dont_stop_watching(self):
        self.mode("Focus", {"not_busy"}, 10)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("deep work", self.at(30), self.at(90), free=True))
        self.assertEqual(self.follow(), ("Normal", self.at(20), "Focus"))
        self.assertEqual(self.follow(20)[0], "Focus")
        self.assertIsNone(server.feed("score")["stop_at"])

    def test_a_lower_mode_still_wins_by_priority_during_the_lead_time(self):
        # Normal above Meeting: free time ends when the meeting's lead time begins, so Meeting takes over.
        self.mode("Normal", {"free_time"}, 0)
        self.mode("Meeting", {"busy"}, 30)
        self.set_events(event("standup", self.at(60), self.at(90)))
        self.assertEqual(self.follow(), ("Normal", self.at(30), "Meeting"))
        # A higher mode on for something else during the lead time keeps priority.
        self.mode("Focus", {"not_busy"}, 0)
        server.move_mode("Focus", 0)
        self.set_events(event("standup", self.at(60), self.at(90)), event("deep work", self.at(0), self.at(45), free=True))
        self.assertEqual(self.follow(), ("Focus", self.at(45), "Meeting"))

    def test_a_new_mode_has_no_lead_time(self):
        self.assertEqual(ranking.DEFAULT_TERMS["event_lead_minutes"], 0)
        self.assertIsNone(server.add_mode("Meeting"))
        self.assertIsNone(server.set_mode_when("Meeting", {"busy": True}))
        self.assertIsNone(server.choose_mode(None))
        self.assertEqual(server.mode_terms("Meeting")["event_lead_minutes"], 0)
        self.set_events(event("standup", self.at(60), self.at(90)))
        self.assertEqual(self.follow(), (None, self.at(60), "Meeting"))
        self.assertEqual(server.feed("score")["stop_at"], self.at(60))

    def test_lead_time_zero_starts_the_mode_with_the_event(self):
        self.mode("Meeting", {"busy"}, 0)
        self.set_events(event("standup", self.at(60), self.at(90)))
        self.assertEqual(self.follow(), (None, self.at(60), "Meeting"))
        self.assertEqual(server.feed("score")["stop_at"], self.at(60))

    def test_previewing_the_lead_time_of_the_mode_in_effect(self):
        self.mode("Meeting", {"busy"}, 30)
        self.mode("Play", set(), 0)
        self.set_events(event("standup", self.at(60), self.at(90)))
        server.choose_mode("Meeting")
        preview = dict(server.terms_for("Meeting"), event_lead_minutes=50)
        self.assertEqual(server.feed("score", terms=preview)["stop_at"], self.at(10))
        self.assertEqual(server.mode_terms("Meeting")["event_lead_minutes"], 30)  # not saved
        self.assertEqual(server.feed("score")["stop_at"], self.at(30))
        # Previewing another mode's lead time doesn't change the meeting's.
        server.choose_mode("Play")
        self.assertEqual(server.feed("score", terms=dict(server.terms_for("Play"), event_lead_minutes=50))["stop_at"], self.at(30))

    def test_the_stop_screen_names_the_events_calendar(self):
        self.mode("Meeting", {"busy"}, 30)
        self.set_events(event("standup", self.at(10), self.at(40)))
        self.assertEqual(server.feed("score")["stop_event"]["calendar"], "Calendar")


class NextModeChangeTests(unittest.TestCase):
    """Following the calendar, until is when the mode next changes, and next the mode then."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, "modes", []))
        self.stack.enter_context(patch.object(server, "mode_choice", {}))

    def use(self, *spec):
        """Modes, highest priority first, as (name, situations it's on in[, lead time in minutes, 0 if not
        given])."""
        server.modes[:] = [{"name": name, "when": {s: s in on for s in server.SITUATIONS},
                            "terms": {"event_lead_minutes": lead[0] if lead else 0}} for name, on, *lead in spec]

    def change(self, events, now=NOW):
        mode = server.active_mode(events, now)
        return mode["name"], mode["until"], mode["next"]

    def test_a_higher_priority_mode_starting_during_an_event_ends_the_mode_then(self):
        self.use(("Busy", {"busy"}), ("Work", {"not_busy"}))
        events = [event("Work block", NOW - 60, NOW + 7200, free=True), event("Meeting", NOW + 1800, NOW + 3600)]
        self.assertEqual(self.change(events), ("Work", NOW + 1800, "Busy"))
        # During the meeting, Work comes back when it ends; at its very start, Busy is on.
        self.assertEqual(self.change(events, NOW + 1800), ("Busy", NOW + 3600, "Work"))
        self.assertEqual(self.change(events, NOW + 3600), ("Work", NOW + 7200, None))

    def test_a_lower_priority_event_starting_changes_nothing(self):
        self.use(("Busy", {"busy"}), ("Work", {"not_busy"}))
        events = [event("Meeting", NOW - 60, NOW + 3600), event("Work block", NOW + 1800, NOW + 7200, free=True)]
        self.assertEqual(self.change(events), ("Busy", NOW + 3600, "Work"))

    def test_with_no_mode_on_until_is_when_one_turns_on(self):
        self.use(("Work", {"not_busy"}))
        events = [event("Work block", NOW + 1200, NOW + 7200, free=True)]
        self.assertEqual(self.change(events), (None, NOW + 1200, "Work"))
        self.assertEqual(server.active_mode(events, NOW)["source"], "none")
        self.assertEqual(self.change([]), (None, None, None))

    def test_a_mode_on_across_events_has_no_change(self):
        self.use(("Normal", {"free_time", "busy"}))
        self.assertEqual(self.change([event("Meeting", NOW + 3600, NOW + 5400)]), ("Normal", None, None))
        self.use(("Normal", {"free_time", "busy", "not_busy"}))
        self.assertEqual(self.change([event("Meeting", NOW + 3600, NOW + 5400),
                                      event("Work", NOW + 5400, NOW + 9000, free=True)]), ("Normal", None, None))

    def test_back_to_back_events_turning_on_the_same_mode_count_as_one(self):
        self.use(("Work", {"not_busy"}))
        events = [event("A", NOW - 60, NOW + 3600, free=True), event("B", NOW + 3600, NOW + 7200, free=True),
                  event("C", NOW + 7000, NOW + 9000, free=True)]
        self.assertEqual(self.change(events), ("Work", NOW + 9000, None))

    def test_free_time_ends_at_the_next_event_only_if_the_mode_changes(self):
        self.use(("Normal", {"free_time"}))
        self.assertEqual(self.change([event("Work block", NOW + 3600, NOW + 7200, free=True)]), ("Normal", NOW + 3600, None))
        self.use(("Normal", {"free_time"}), ("Focus", {"not_busy"}))
        self.assertEqual(self.change([event("Work block", NOW + 3600, NOW + 7200, free=True)]), ("Normal", NOW + 3600, "Focus"))
        # At the end of the block, free time again: Normal comes back.
        self.assertEqual(self.change([event("Work block", NOW + 3600, NOW + 7200, free=True)], NOW + 3600),
                         ("Focus", NOW + 7200, "Normal"))

    def test_all_day_events_tasks_and_events_without_an_end_are_left_out(self):
        self.use(("Normal", {"free_time"}), ("Busy", {"busy"}))
        birthday = dict(event("Birthday", NOW + 600, NOW + 86400), all_day=True)
        task = dict(event("Task", NOW + 600, NOW + 1200), calendar_kind="task")
        call = event("Call", NOW + 900, None)
        self.assertEqual(self.change([birthday, task, call]), ("Normal", None, None))
        self.assertEqual(self.change([birthday, task, call, event("Meeting", NOW + 1800, NOW + 3600)]),
                         ("Normal", NOW + 1800, "Busy"))

    def test_changes_are_looked_for_a_week_ahead(self):
        self.use(("Normal", {"free_time"}), ("Busy", {"busy"}))
        week = server.MODE_LOOKAHEAD
        self.assertEqual(self.change([event("Trip", NOW + week, NOW + week + 3600)]), ("Normal", NOW + week, "Busy"))
        self.assertEqual(self.change([event("Trip", NOW + week + 1, NOW + week + 3600)]), ("Normal", None, None))

    def test_a_chosen_mode_doesnt_change(self):
        self.use(("Work", {"not_busy"}), ("Play", set()))
        server.mode_choice["name"] = "Play"
        self.assertEqual(self.change([event("Work block", NOW + 600, NOW + 3600, free=True)]), ("Play", None, None))

    def test_the_feed_and_settings_report_it(self):
        with patch.object(server, "calendar_events", return_value=[event("Work block", time.time() + 600, time.time() + 3600, free=True)]):
            self.use(("Work", {"not_busy"}))
            mode = server.settings()["mode"]
        self.assertEqual((mode["name"], mode["next"]), (None, "Work"))
        self.assertAlmostEqual(mode["until"], time.time() + 600, delta=5)


if __name__ == "__main__":
    unittest.main()
