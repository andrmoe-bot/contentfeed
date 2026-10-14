import copy
import random
import unittest
from unittest.mock import patch

import ranking


NOW = 2_000_000


def item(url, sub=None, age=None, date=NOW):
    return {"url": url, "title": url, "sub": sub, "date": date,
            "last_viewed": None if age is None else NOW - age * 3600,
            "tags": [], "kind": "subscription" if sub else "link",
            "feed": sub, "position": -1, "color": "white"}


def brute_force(items, weights):
    """The original all-pairs algorithm, with tag similarity removed."""
    recent = [it for it in items if it["last_viewed"] is not None
              and ranking.hours_since(NOW, it["last_viewed"]) < weights["similar_hours"]]
    for it in items:
        best = None
        if weights["similar_penalty"]:
            for other in recent:
                percent, why = ranking.similarity(it, other, weights)
                hours = ranking.hours_since(NOW, other["last_viewed"])
                strength = percent * (1 - hours / weights["similar_hours"])
                if strength and (not best or strength > best["strength"]
                                 or (strength == best["strength"] and why == "itself")):
                    best = {"title": other["title"], "hours": hours, "percent": percent,
                            "strength": strength, "why": why, "skipped": other.get("skipped", False)}
        it["similar"] = best


class SimilarityTests(unittest.TestCase):
    def test_index_matches_brute_force(self):
        rng = random.Random(42)
        items = [item(str(i), rng.choice([None, "a", "b", "c"]),
                      rng.choice([None, -1, 0, 1, 12, 23, 24, 48]),
                      NOW - rng.randrange(100) * 86400) for i in range(200)]
        for it in items:
            it["skipped"] = rng.choice([False, True])
        for hours, days, penalty in [(24, 30, 40), (1.5, 0.5, 40), (0, 30, 40), (24, 0, 40), (24, 30, 0)]:
            with self.subTest(hours=hours, days=days, penalty=penalty):
                w = dict(ranking.DEFAULT_WEIGHTS, similar_hours=hours,
                         similar_days=days, similar_penalty=penalty)
                expected, actual = copy.deepcopy(items), copy.deepcopy(items)
                brute_force(expected, w)
                ranking.similar_to_recent(actual, NOW, w)
                self.assertEqual(actual, expected)

    def test_linear_decay_and_cutoff(self):
        w = dict(ranking.DEFAULT_WEIGHTS, seen_bonus=0)
        for age, expected in [(0, -40), (6, -30), (12, -20), (23, -1.67), (24, 0), (25, 0)]:
            with self.subTest(age=age):
                it = item("self", age=age)
                ranking.similar_to_recent([it], NOW, w)
                self.assertEqual(sum(p for _, p in ranking.score(it, NOW, w)), expected)

    def test_recent_weaker_similarity_can_win(self):
        candidate = item("candidate", "a")
        old = item("old", "a", age=23)
        fresh = item("fresh", "a", age=0, date=NOW - 15 * 86400)
        ranking.similar_to_recent([candidate, old, fresh], NOW, ranking.DEFAULT_WEIGHTS)
        self.assertEqual(candidate["similar"]["title"], "fresh")
        self.assertEqual(candidate["similar"]["strength"], 50)

    def test_self_wins_ties(self):
        first, second = item("first", "a", 0), item("second", "a", 0)
        ranking.similar_to_recent([first, second], NOW, ranking.DEFAULT_WEIGHTS)
        self.assertEqual(first["similar"]["why"], "itself")
        self.assertEqual(second["similar"]["why"], "itself")

    def test_shared_tags_do_not_match(self):
        first, second = item("first", "a"), item("second", "b", 0)
        first["tags"] = second["tags"] = ["science"]
        ranking.similar_to_recent([first, second], NOW, ranking.DEFAULT_WEIGHTS)
        self.assertIsNone(first["similar"])

    def test_only_same_subscription_is_compared_at_scale(self):
        items = [item(str(i), str(i // 200)) for i in range(12000)]
        for i in range(100):
            items[i * 113]["last_viewed"] = NOW
        with patch.object(ranking, "similarity", wraps=ranking.similarity) as compare:
            ranking.similar_to_recent(items, NOW, ranking.DEFAULT_WEIGHTS)
        self.assertEqual(compare.call_count, 20_000)  # formerly 1,200,000 comparisons


if __name__ == "__main__":
    unittest.main()
