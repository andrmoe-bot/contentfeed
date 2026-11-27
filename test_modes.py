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

OWN = dict(ranking.DEFAULT_TERMS)  # a mode's default settings


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
        self.stack.enter_context(patch.object(server, "schedule", return_value=None))
        self.stack.enter_context(patch.object(server, "schedule_subscription", return_value=None))
        self.feed_url, self.cal_url = "https://example.com/feed", "https://example.com/cal.ics"
        server.FEEDS_FILE.write_text(f"{self.feed_url}\n{self.cal_url}\n")
        post = {"url": "https://example.com/post", "title": "Post", "first_seen": 1_000_000, "published": 1_000_000}
        server.subscriptions[self.feed_url] = {"title": "Example", "feed_url": self.feed_url, "items": [post], "fetched": 1}
        server.cache[post["url"]] = {"url": post["url"], "fetched": 1, "length_version": 1}  # read, so in the feed
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
        result = server.feed("score")["mode"]
        self.assertEqual({k: v for k, v in result.items() if k != "trigger"},
                         {"name": "Work", "source": "calendar", "situation": "not_busy",
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
        self.assertEqual({k: v for k, v in mode().items() if k != "trigger"},
                         {"name": "Idle", "source": "calendar", "situation": "free_time", "event": None,
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

    def test_the_trigger_is_the_event_that_turned_the_mode_on(self):
        self.assertIsNone(server.add_mode("Busy"))
        self.assertIsNone(server.add_mode("Idle"))
        server.set_mode_when("Busy", {"busy": True})
        server.set_mode_when("Idle", {"free_time": True})
        server.find_mode("Busy")["terms"]["event_lead_minutes"] = 10
        self.assertIsNone(server.choose_mode(None))
        now = time.time()
        dinner = dict(event("dinner", now + 300, now + 3600, title="Dinner"), description="At home")
        self.set_events(dinner)
        # In the lead time, the event turns Busy on before it starts.
        trigger = server.feed("score")["mode"]["trigger"]
        self.assertEqual(trigger, {"url": dinner["url"], "title": "Dinner", "description": "At home",
                                   "calendar_start": now + 300, "deadline": now + 3600, "calendar": "Calendar"})
        # In free time, the trigger is the event that ends it.
        self.set_events(dict(dinner, calendar_start=now + 7200, deadline=now + 9000))
        mode = server.feed("score")["mode"]
        self.assertEqual((mode["name"], mode["trigger"]["title"], mode["until"]), ("Idle", "Dinner", now + 6600))
        self.set_events()
        self.assertEqual(server.feed("score")["mode"]["trigger"], None)
        self.assertIsNone(server.choose_mode("Busy"))
        self.assertEqual(server.feed("score")["mode"]["trigger"], None)

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
        self.assertEqual((result["mode"], result["items"], result["post_limit"], result["total"]), ({"name": None, "source": "none", "situation": None, "event": None, "trigger": None, "until": None, "next": None}, [], 0, 1))
        # A subscription's page still lists its posts, ranked with the default settings.
        self.assertEqual(len(server.feed("score", subscription_url=self.feed_url)["items"]), 1)
        self.assertEqual(server.settings()["mode"], {"name": None, "source": "none", "situation": None, "event": None, "trigger": None, "until": None, "next": None})

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
                                                     "event": None, "trigger": None, "until": None, "next": None})
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



class ModeSettingsTests(ServerTestCase):
    """Fetch details is the same in every mode, and applies with none on."""

    def setUp(self):
        super().setUp()
        for name in ("Work", "Play"):
            self.assertIsNone(server.add_mode(name))
        self.assertIsNone(server.choose_mode("Work"))

    def save(self, mode=None, **changes):
        return server.save_settings(dict(ranking.DEFAULT_TERMS, **changes), mode)

    def test_every_setting_belongs_to_a_mode(self):
        data = server.settings()
        self.assertNotIn("global", data)
        for mode in data["modes"]:
            self.assertEqual(mode["terms"], OWN)
        self.assertIsNone(self.save("Work", video_bonus=4))
        self.assertEqual(server.mode_terms("Work")["video_bonus"], 4)
        self.assertEqual(server.mode_terms("Play"), OWN)

    def test_with_no_mode_on_nothing_is_saved(self):
        self.assertIsNone(server.choose_mode(None))
        self.assertIsNone(server.feed("score")["mode"]["name"])
        before = [dict(m["terms"]) for m in server.modes]
        self.assertIsNotNone(self.save(video_bonus=99))
        self.assertEqual([m["terms"] for m in server.modes], before)

    def test_invalid_settings_change_nothing(self):
        for bad in ({"video_bonus": "x"}, {"event_lead_minutes": -1}, {"post_limit": 1.5}):
            self.assertIsNotNone(self.save("Work", **bad), bad)
            self.assertEqual(server.mode_terms("Work"), OWN)
        self.assertIsNotNone(server.save_settings(dict(OWN, enrich_count=5), "Work"))  # no longer a setting
        missing = {k: v for k, v in OWN.items() if k != "video_bonus"}
        self.assertIsNotNone(server.save_settings(missing, "Work"))
        self.assertEqual(self.save("Gone", video_bonus=9), "there's no mode called Gone")

    def test_the_old_global_fetch_details_is_ignored(self):
        self.load({"version": 5, "global": {"enrich_count": 6},
                   "modes": [{"name": "Work", "when": {}, "terms": dict(OWN, enrich_count=3, video_bonus=2)}]})
        self.assertEqual(server.mode_terms("Work"), dict(OWN, video_bonus=2))
        self.assertIsNone(self.save("Work", video_bonus=2))
        saved = json.loads(server.SETTINGS_FILE.read_text())
        self.assertEqual(saved["version"], server.SETTINGS_VERSION)
        self.assertNotIn("global", saved)

    def test_details_are_fetched_for_the_page_whatever_the_mode(self):
        for choice in ("Work", "Play", None):
            server.choose_mode(choice)
            with patch.object(server, "enrich_posts") as enrich:
                server.feed("score", enrich=True, subscription_url=self.feed_url)
            enrich.assert_called_once()

    def load(self, data):
        server.SETTINGS_FILE.write_text(json.dumps(data))
        server.load_settings()

    def test_older_settings_keep_each_modes_lead_time(self):
        mode = lambda name, **terms: {"name": name, "when": {"free_time": True}, "terms": dict(ranking.DEFAULT_TERMS, **terms)}
        self.load({"version": 3, "modes": [mode("Work", enrich_count=1, event_lead_minutes=5),
                                           mode("Normal", enrich_count=7, event_lead_minutes=50)]})
        self.assertEqual([m["terms"] for m in server.modes], [dict(OWN, event_lead_minutes=5), dict(OWN, event_lead_minutes=50)])
        # Before version 3, the normal settings become the Normal mode, with their lead time.
        self.load({"version": 2, "terms": dict(ranking.DEFAULT_TERMS, event_lead_minutes=40, enrich_count=2), "modes": []})
        self.assertEqual(server.mode_terms("Normal"), dict(OWN, event_lead_minutes=40))

    def test_the_past_stop_points_become_the_past_mode_change_points(self):
        old = {k: v for k, v in ranking.DEFAULT_TERMS.items() if k != "past_mode_change_per_minute"}
        mode = {"name": "Work", "when": {"busy": True}, "terms": dict(old, past_stop_per_minute=-7)}
        self.load({"version": 4, "global": {}, "modes": [mode]})
        self.assertEqual(server.mode_terms("Work")["past_mode_change_per_minute"], -7)
        self.assertNotIn("past_stop_per_minute", server.mode_terms("Work"))
        self.load({"version": 2, "terms": dict(old, past_stop_per_minute=-3), "modes": []})
        self.assertEqual(server.mode_terms("Normal")["past_mode_change_per_minute"], -3)

    def test_the_api_saves_a_modes_settings(self):
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, video_bonus=8), "mode": "Play"})
        self.assertEqual((code, next(m for m in body["modes"] if m["name"] == "Play")["terms"]["video_bonus"]), (200, 8))
        self.assertNotIn("global", body)
        body, code = server.settings_api({"terms": dict(ranking.DEFAULT_TERMS, enrich_count=1), "mode": "Play"})
        self.assertEqual(code, 400)


class LeadTimeTests(ServerTestCase):
    """A mode's lead time: the mode turns on that long before an event it's on for."""

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

    def test_the_mode_changes_with_the_lead_time_of_the_mode_the_event_turns_on(self):
        self.mode("Meeting", {"busy"}, 30)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("standup", self.at(60), self.at(90)))
        result = server.feed("score")
        self.assertEqual((result["mode"]["name"], result["mode"]["until"]), ("Normal", self.at(30)))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=10), "Meeting"))
        self.assertEqual(self.follow(), ("Normal", self.at(50), "Meeting"))
        self.assertIsNone(server.save_settings(dict(ranking.DEFAULT_TERMS, event_lead_minutes=120), "Normal"))
        self.assertEqual(self.follow(), ("Normal", self.at(50), "Meeting"))  # Normal isn't on during busy events

    def test_an_event_with_no_mode_for_it_ends_free_time_at_its_start(self):
        self.mode("Normal", {"free_time"}, 30)  # free time doesn't use the lead time
        self.set_events(event("dentist", self.at(60), self.at(120)))
        self.assertEqual(self.follow(), ("Normal", self.at(60), None))

    def test_the_highest_mode_on_for_the_event_sets_the_lead_time(self):
        self.mode("Meeting", {"busy"}, 15)
        self.mode("Calls", {"busy"}, 45)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("call", self.at(60), self.at(90)))
        self.assertEqual(self.follow(), ("Normal", self.at(45), "Meeting"))
        server.move_mode("Calls", 0)
        self.assertEqual(self.follow(), ("Normal", self.at(15), "Calls"))

    def test_free_events_turn_their_mode_on_early(self):
        self.mode("Focus", {"not_busy"}, 10)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("deep work", self.at(30), self.at(90), free=True))
        self.assertEqual(self.follow(), ("Normal", self.at(20), "Focus"))
        self.assertEqual(self.follow(20)[0], "Focus")

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

    def test_lead_time_zero_starts_the_mode_with_the_event(self):
        self.mode("Meeting", {"busy"}, 0)
        self.set_events(event("standup", self.at(60), self.at(90)))
        self.assertEqual(self.follow(), (None, self.at(60), "Meeting"))

    def test_previewing_the_lead_time_moves_the_mode_change_and_its_points(self):
        # Back-to-back meetings: a 30-minute lead bridges the gap, so Meeting stays on until the second ends.
        self.mode("Meeting", {"busy"}, 30)
        self.mode("Normal", {"free_time"}, 0)
        self.set_events(event("m1", self.at(-10), self.at(10)), event("m2", self.at(30), self.at(60)))
        long = {"url": "https://example.com/long", "title": "Long", "first_seen": 1_000_000,
                "published": 1_000_000, "estimated_minutes": 45}
        server.subscriptions[self.feed_url]["items"].append(long)
        past = lambda result: [r["label"] for it in result["items"] if it["url"] == long["url"]
                               for r in it["reasons"] if "past the mode change" in r["label"]]
        saved = server.feed("score")
        self.assertEqual((saved["mode"]["until"], past(saved)), (self.at(60), []))
        # A 10-minute lead opens a gap from 10 to 20 minutes from now: the mode changes at the first's end.
        preview = server.feed("score", terms=dict(server.terms_for("Meeting"), event_lead_minutes=10))
        self.assertEqual((preview["mode"]["until"], preview["mode"]["next"]), (self.at(10), "Normal"))
        self.assertEqual(len(past(preview)), 1)
        self.assertIn(past(preview)[0], ("runs 35 minutes past the mode change", "runs 36 minutes past the mode change"))
        self.assertEqual(server.feed("score")["mode"]["until"], self.at(60))  # not saved



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
