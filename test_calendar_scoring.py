import unittest

import ranking
from test_ranking import NOW, item


class CalendarScoringTests(unittest.TestCase):
    def test_event_bonus_only_ramps_during_the_final_hour(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, past_event_penalty=0)
        for seconds, expected in ((3600, 0), (1800, 100), (0, 200), (-1, 0)):
            event = dict(item("event"), calendar_kind="event", calendar_start=NOW + seconds)
            reasons = ranking.score(event, NOW, weights)
            self.assertEqual(sum(points for _, points in reasons), expected)

    def test_event_and_task_urgency_amounts_are_configurable(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, event_bonus=80, task_bonus=80)
        event = dict(item("event"), calendar_kind="event", calendar_start=NOW + 1800)
        task = dict(item("task"), calendar_kind="task", deadline=NOW + 15 * 86400)
        self.assertEqual(ranking.score(event, NOW, weights)[0][1], 40)
        self.assertEqual(ranking.score(task, NOW, weights)[0][1], 40)

    def test_past_event_penalty_starts_after_event_end(self):
        event = dict(item("event"), calendar_kind="event", calendar_start=NOW - 3600, deadline=NOW + 3600)
        ongoing = ranking.score(event, NOW, ranking.DEFAULT_WEIGHTS)
        self.assertFalse(any(label == "event has ended" for label, _ in ongoing))
        event["deadline"] = NOW - 1
        past = ranking.score(event, NOW, ranking.DEFAULT_WEIGHTS)
        self.assertIn(("event has ended", -100), past)

    def test_task_recent_similarity_uses_tasks_from_its_calendar_only(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened_task = dict(item("opened-task", "calendar", age=1), calendar_kind="task")
        opened_event = dict(item("opened-event", "calendar", age=1), calendar_kind="event")
        ranking.similar_to_recent([task, opened_task, opened_event], NOW, ranking.WEIGHTS)
        self.assertEqual(task["similar"]["url"], "opened-task")

    def test_task_urgency_scales_to_its_deadline_and_is_optional(self):
        for deadline, expected in ((None, 0), (NOW + 30 * 86400, 0),
                                   (NOW + 15 * 86400, 100), (NOW, 200), (NOW - 1, 200)):
            task = dict(item("task"), calendar_kind="task", deadline=deadline)
            reasons = ranking.score(task, NOW, ranking.DEFAULT_WEIGHTS)
            self.assertEqual(sum(points for _, points in reasons), expected)

    def test_task_score_applies_recent_task_similarity(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened = dict(item("opened", "calendar", age=1), calendar_kind="task")
        ranking.similar_to_recent([task, opened], NOW, ranking.WEIGHTS)
        reasons = ranking.score(task, NOW, ranking.WEIGHTS)
        self.assertEqual(len(reasons), 1)
        self.assertLess(reasons[0][1], 0)
        self.assertIn("like “opened”", reasons[0][0])

    def test_above_similarity_spreads_events_and_tasks_within_the_calendar(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_penalty=0, above_similarity_penalty=-15)
        for kind in ("event", "task"):
            with self.subTest(kind=kind):
                first = dict(item("first", "calendar"), calendar_kind=kind)
                second = dict(item("second", "calendar"), calendar_kind=kind)
                ranked = ranking.rank("score", [first, second], NOW, weights)
                self.assertEqual(sorted(it["score"] for it in ranked), [-15, 0])
                penalized = next(it for it in ranked if it["score"] == -15)
                self.assertEqual(penalized["reasons"][-1]["label"], "similarity to 1 post above, 100% total")
                explained = ranking.explain_similarities(ranked, ranked, weights)
                explained_penalized = next(it for it in explained if it["score"] == -15)
                self.assertEqual([match["title"] for match in explained_penalized["above_matches"]], ["first"])

    def test_events_stay_out_of_task_similarity(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_penalty=0, above_similarity_penalty=-15)
        task = dict(item("task", "calendar"), calendar_kind="task")
        event = dict(item("event", "calendar"), calendar_kind="event")
        ranked = ranking.rank("score", [task, event], NOW, weights)
        self.assertEqual([it["score"] for it in ranked], [0, 0])


if __name__ == "__main__":
    unittest.main()
