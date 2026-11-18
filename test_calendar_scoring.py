import unittest

import ranking
from test_ranking import NOW, item


class CalendarScoringTests(unittest.TestCase):
    def test_events_do_not_ramp_as_they_approach(self):
        """Events work through the stop-time filter (see effective_stop), not through score points."""
        terms = dict(ranking.DEFAULT_TERMS, past_event_penalty=0)
        scores = set()
        for seconds in (60, 1800, 3600, 7 * 86400):
            event = dict(item("event"), calendar_kind="event", calendar_start=NOW + seconds)
            scores.add(sum(points for _, points in ranking.score(event, NOW, terms)))
        self.assertEqual(scores, {0})

    def test_task_urgency_amount_is_configurable(self):
        terms = dict(ranking.DEFAULT_TERMS, task_deadline_bonus=80)
        task = dict(item("task"), calendar_kind="task", deadline=NOW + 15 * 86400)
        self.assertEqual(ranking.score(task, NOW, terms)[0][1], 40)

    def test_event_and_task_fixed_terms_default_to_zero_and_accept_penalties(self):
        self.assertEqual(ranking.DEFAULT_TERMS["event_bonus"], 0)
        self.assertEqual(ranking.DEFAULT_TERMS["task_bonus"], 0)
        event = dict(item("event"), calendar_kind="event", calendar_start=NOW + 7200)
        task = dict(item("task"), calendar_kind="task")
        for kind, item_ in (("event", event), ("task", task)):
            with self.subTest(kind=kind):
                self.assertFalse(any(label == kind for label, _ in ranking.score(item_, NOW, ranking.DEFAULT_TERMS)))
                for value in (-25, 25):
                    reasons = ranking.score(item_, NOW, dict(ranking.DEFAULT_TERMS, **{f"{kind}_bonus": value}))
                    self.assertIn((kind, value), reasons)

    def test_past_event_penalty_starts_after_event_end(self):
        event = dict(item("event"), calendar_kind="event", calendar_start=NOW - 3600, deadline=NOW + 3600)
        ongoing = ranking.score(event, NOW, ranking.DEFAULT_TERMS)
        self.assertFalse(any(label == "event has ended" for label, _ in ongoing))
        event["deadline"] = NOW - 1
        past = ranking.score(event, NOW, ranking.DEFAULT_TERMS)
        self.assertIn(("event has ended", -100), past)

    def test_task_recent_similarity_uses_tasks_from_its_calendar_only(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened_task = dict(item("opened-task", "calendar", age=1), calendar_kind="task")
        opened_event = dict(item("opened-event", "calendar", age=1), calendar_kind="event")
        ranking.similar_to_recent([task, opened_task, opened_event], NOW, ranking.TERMS)
        self.assertEqual(task["similar"]["url"], "opened-task")

    def test_task_urgency_scales_to_its_deadline_and_is_optional(self):
        for deadline, expected in ((None, 0), (NOW + 30 * 86400, 0),
                                   (NOW + 15 * 86400, 100), (NOW, 200), (NOW - 1, 200)):
            task = dict(item("task"), calendar_kind="task", deadline=deadline)
            reasons = ranking.score(task, NOW, ranking.DEFAULT_TERMS)
            self.assertEqual(sum(points for _, points in reasons), expected)

    def test_task_score_applies_recent_task_similarity(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened = dict(item("opened", "calendar", age=1), calendar_kind="task")
        ranking.similar_to_recent([task, opened], NOW, ranking.TERMS)
        reasons = ranking.score(task, NOW, ranking.TERMS)
        self.assertEqual(len(reasons), 1)
        self.assertLess(reasons[0][1], 0)
        self.assertIn("like “opened”", reasons[0][0])

    def test_above_similarity_spreads_events_and_tasks_within_the_calendar(self):
        terms = dict(ranking.DEFAULT_TERMS, similar_penalty=0, above_similarity_penalty=-15)
        for kind in ("event", "task"):
            with self.subTest(kind=kind):
                first = dict(item("first", "calendar"), calendar_kind=kind)
                second = dict(item("second", "calendar"), calendar_kind=kind)
                ranked = ranking.rank("score", [first, second], NOW, terms)
                self.assertEqual(sorted(it["score"] for it in ranked), [-15, 0])
                penalized = next(it for it in ranked if it["score"] == -15)
                self.assertEqual(penalized["reasons"][-1]["label"], "similarity to 1 post above, 100% total")
                explained = ranking.explain_similarities(ranked, ranked, terms)
                explained_penalized = next(it for it in explained if it["score"] == -15)
                self.assertEqual([match["title"] for match in explained_penalized["above_matches"]], ["first"])

    def test_events_stay_out_of_task_similarity(self):
        terms = dict(ranking.DEFAULT_TERMS, similar_penalty=0, above_similarity_penalty=-15)
        task = dict(item("task", "calendar"), calendar_kind="task")
        event = dict(item("event", "calendar"), calendar_kind="event")
        ranked = ranking.rank("score", [task, event], NOW, terms)
        self.assertEqual([it["score"] for it in ranked], [0, 0])


if __name__ == "__main__":
    unittest.main()
