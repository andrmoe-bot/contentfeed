import unittest

import ranking
from test_ranking import NOW, item


class CalendarScoringTests(unittest.TestCase):
    def test_event_bonus_only_ramps_during_the_final_hour(self):
        for seconds, expected in ((3600, 0), (1800, 100), (0, 200), (-1, 0)):
            event = dict(item("event"), calendar_kind="event", calendar_start=NOW + seconds)
            reasons = ranking.score(event, NOW, ranking.DEFAULT_WEIGHTS)
            self.assertEqual(sum(points for _, points in reasons), expected)

    def test_task_recent_similarity_uses_tasks_from_its_calendar_only(self):
        task = dict(item("task", "calendar"), calendar_kind="task")
        opened_task = dict(item("opened-task", "calendar", age=1), calendar_kind="task")
        opened_event = dict(item("opened-event", "calendar", age=1), calendar_kind="event")
        ranking.similar_to_recent([task, opened_task, opened_event], NOW, ranking.WEIGHTS)
        self.assertEqual(task["similar"]["url"], "opened-task")

    def test_above_similarity_spreads_tasks_within_the_calendar(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_penalty=0, above_similarity_penalty=-15)
        first = dict(item("first", "calendar"), calendar_kind="task")
        second = dict(item("second", "calendar"), calendar_kind="task")
        ranked = ranking.rank("score", [first, second], NOW, weights)
        self.assertEqual(sorted(it["score"] for it in ranked), [-15, 0])

    def test_events_stay_out_of_task_similarity(self):
        weights = dict(ranking.DEFAULT_WEIGHTS, similar_penalty=0, above_similarity_penalty=-15)
        task = dict(item("task", "calendar"), calendar_kind="task")
        event = dict(item("event", "calendar"), calendar_kind="event")
        ranked = ranking.rank("score", [task, event], NOW, weights)
        self.assertEqual([it["score"] for it in ranked], [0, 0])


if __name__ == "__main__":
    unittest.main()
