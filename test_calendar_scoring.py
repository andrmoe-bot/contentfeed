import unittest

import ranking
from test_ranking import NOW, item, TERMS_ON


class CalendarScoringTests(unittest.TestCase):
    def test_task_urgency_amount_is_configurable(self):
        terms = dict(TERMS_ON, task_deadline_bonus=80)
        task = dict(item("task"), calendar_kind="task", deadline=NOW + 15 * 86400)
        self.assertEqual(ranking.score(task, NOW, terms)[0][1], 40)

    def test_task_fixed_term_defaults_to_zero_and_accepts_penalties(self):
        self.assertEqual(ranking.DEFAULT_TERMS["task_bonus"], 0)
        task = dict(item("task"), calendar_kind="task")
        self.assertFalse(any(label == "task" for label, _ in ranking.score(task, NOW, TERMS_ON)))
        for value in (-25, 25):
            reasons = ranking.score(task, NOW, dict(TERMS_ON, task_bonus=value))
            self.assertIn(("task", value), reasons)

    def test_task_priority_scales_from_priority_1_and_defaults_to_off(self):
        self.assertEqual(ranking.DEFAULT_TERMS["task_priority_bonus"], 0)
        terms = dict(TERMS_ON, task_deadline_bonus=0, task_priority_bonus=90)
        for priority, expected in ((1, 90), (5, 50), (9, 10), (None, 0), (0, 0)):
            task = dict(item("task"), calendar_kind="task", priority=priority)
            reasons = ranking.score(task, NOW, terms)
            self.assertEqual(sum(points for _, points in reasons), expected)
            if expected:
                self.assertEqual(reasons, [(f"priority {priority}", expected)])
        task = dict(item("task"), calendar_kind="task", priority=1)
        self.assertEqual(ranking.score(task, NOW, TERMS_ON), [])
        self.assertEqual(ranking.score(task, NOW, dict(terms, task_priority_bonus=-90)), [("priority 1", -90)])

    def test_task_recent_similarity_uses_tasks_from_its_calendar_only(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened_task = dict(item("opened-task", "calendar", age=1), calendar_kind="task")
        opened_event = dict(item("opened-event", "calendar", age=1), calendar_kind="event")
        ranking.similar_to_recent([task, opened_task, opened_event], NOW, TERMS_ON)
        self.assertEqual(task["similar"]["url"], "opened-task")

    def test_task_urgency_scales_to_its_deadline_and_is_optional(self):
        for deadline, expected in ((None, 0), (NOW + 30 * 86400, 0),
                                   (NOW + 15 * 86400, 100), (NOW, 200), (NOW - 1, 200)):
            task = dict(item("task"), calendar_kind="task", deadline=deadline)
            reasons = ranking.score(task, NOW, TERMS_ON)
            self.assertEqual(sum(points for _, points in reasons), expected)

    def test_overdue_task_says_so_and_keeps_the_full_points(self):
        task = dict(item("task"), calendar_kind="task", deadline=NOW - 2 * 86400 - 5 * 3600)
        self.assertEqual(ranking.score(task, NOW, TERMS_ON),
                         [("task overdue by 2 days 5 hours, full deadline points until done", 200)])
        due_soon = dict(item("task"), calendar_kind="task", deadline=NOW + 3 * 3600)
        self.assertEqual(ranking.score(due_soon, NOW, TERMS_ON)[0][0], "task deadline in 3 hours")

    def test_task_deadline_ramp_starts_at_its_setting(self):
        terms = dict(TERMS_ON, task_deadline_days=10)
        for deadline, expected in ((NOW + 11 * 86400, 0), (NOW + 10 * 86400, 0),
                                   (NOW + 5 * 86400, 100), (NOW, 200)):
            task = dict(item("task"), calendar_kind="task", deadline=deadline)
            self.assertEqual(sum(points for _, points in ranking.score(task, NOW, terms)), expected)
        task = dict(item("task"), calendar_kind="task", deadline=NOW)
        self.assertEqual(ranking.score(task, NOW, dict(terms, task_deadline_days=0)), [])

    def test_task_score_applies_recent_task_similarity(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened = dict(item("opened", "calendar", age=1), calendar_kind="task")
        ranking.similar_to_recent([task, opened], NOW, TERMS_ON)
        reasons = ranking.score(task, NOW, TERMS_ON)
        self.assertEqual(len(reasons), 1)
        self.assertLess(reasons[0][1], 0)
        self.assertIn("like “opened”", reasons[0][0])

    def test_above_similarity_spreads_tasks_within_the_calendar(self):
        terms = dict(TERMS_ON, similar_penalty=0, above_similarity_penalty=-15)
        first = dict(item("first", "calendar"), calendar_kind="task")
        second = dict(item("second", "calendar"), calendar_kind="task")
        ranked = ranking.rank("score", [first, second], NOW, terms)
        self.assertEqual(sorted(it["score"] for it in ranked), [-15, 0])
        penalized = next(it for it in ranked if it["score"] == -15)
        self.assertEqual(penalized["reasons"][-1]["label"], "similarity to 1 post above, 100% total")
        explained = ranking.explain_similarities(ranked, ranked, terms, NOW)
        explained_penalized = next(it for it in explained if it["score"] == -15)
        self.assertEqual([match["title"] for match in explained_penalized["above_matches"]], ["first"])


if __name__ == "__main__":
    unittest.main()
