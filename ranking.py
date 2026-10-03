"""Feed ranking.

Rankers are deliberately simple and explainable: they never learn from how the feed is used.
A ranker looks at one item (with the time now and the score settings) and returns its reasons, each a (label, points) pair, such as
("tagged music", 2). An item's score is the sum of its points, the feed is sorted by score
(highest first; on ties, unopened first, then newest), and every card shows its reasons on the page.

Each item has its page metadata (url, title, description, domain, ...) plus these inputs:

    kind         "link" (from links.txt) or "subscription" (a post from a feed in feeds.txt)
    date         unix time: when a link was added, or when a post was published
    date_approx  True for older YouTube videos, whose date is only known as "3 years ago"
    added        unix time the server first saw the link or post
    tags         words written after the URL on its line in links.txt or feeds.txt, lowercased
    feed         the subscription's title (posts only)
    position     index in links.txt (0 = first line); -1 for posts
    last_viewed  unix time you last opened it, or None
    color        "green", "white" (the default) or "red", as you marked it on the page
    next_after   for a post: the title of the post you opened most recently in its subscription,
                 if this post was published right after that one ("what's next"); otherwise None
    sub          for a post: its subscription's address; None for links
    sub_last_viewed  for a post: unix time you last opened any post in its subscription, or None
                 if you've never opened anything from it

Nothing is hidden: items you've opened or marked red are scored differently, not removed.

A ranker registered with spread=True gets its repeats spread out after scoring (see rank).

Register a ranker with @ranker("name") and select it with `server.py --ranker name`, or try it
without restarting via /api/feed?ranker=name.

Keep points stable between page updates, for example by using whole hours rather than seconds
for age, so the page doesn't redraw every time it checks for changes.
"""

import heapq
import math
from typing import Callable

Reasons = list[tuple[str, float]]
Ranker = Callable[[dict, float, dict], Reasons]
RANKERS: dict[str, Ranker] = {}
DEFAULT = "score"

# Settings for the "score" ranker, in points. Every item is in one group: next up, unseen or seen.
# Times count in steps of ten (see steps), so a time adds at most about 5 × its setting. Settings that
# may be negative say which way: below 0 puts newer (or more recently opened) first, above 0 older.
# These are the defaults; you can change them in the feed's score panel, and server.py keeps your
# values in settings.json.
DEFAULT_WEIGHTS = {
    "next_up_bonus": 30,  # next up: the post after the one you opened most recently in a subscription
    "unseen_bonus": 40,  # unseen: items you've never opened (other than next up)
    "age_per_step": -8,  # unseen: per step of age since published (or added, for links)
    "seen_bonus": -30,  # seen: items you've opened; below 0 puts them under unseen items at first
    "rediscovery_per_step": 12,  # seen: per step of time since you last opened the item
    "new_sub_bonus": 20,  # for posts from a subscription you've never opened anything from
    "repeat_per_post": 15,  # taken off for each post from the same subscription higher up in the feed
    "green_bonus_points": 10,
}
WEIGHTS = dict(DEFAULT_WEIGHTS)
SIGNED = {"age_per_step", "seen_bonus", "rediscovery_per_step"}  # may be negative
LIMIT = 10_000_000
RED = 10_000_000  # red items sink below everything else


def check_weights(weights: dict) -> dict:
    """The weights as numbers, or ValueError if one is missing, unknown, out of range or not a number."""
    if set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError(f"expected exactly these settings: {', '.join(DEFAULT_WEIGHTS)}")
    out = {}
    for key, value in weights.items():
        low = -LIMIT if key in SIGNED else 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= LIMIT:
            raise ValueError(f"{key} must be a number from {low} to {LIMIT}")
        out[key] = int(value) if float(value).is_integer() else value
    return out


def hours_label(hours: int) -> str:
    """Such as "5 hours", "3 days 4 hours", "12 days", "5 months" or "3 years"."""
    days, hours = divmod(hours, 24)
    if days >= 730:
        return f"{days // 365} years"
    if days >= 60:
        return f"{days // 30} months"
    if days >= 7:
        hours = 0
    parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((days, "day"), (hours, "hour")) if n]
    return " ".join(parts) or "0 hours"


def short(title: str) -> str:
    return title if len(title) <= 60 else title[:59] + "…"


def points(x: float) -> float:
    return int(x) if float(x).is_integer() else round(x, 2)


def steps(hours: int) -> float:
    """Time in steps of ten: 1 hour or less is 0, 10 hours 1, 100 hours (4 days) 2, 1000 hours
    (6 weeks) 3, 10 000 hours (14 months) 4, 100 000 hours (11 years) 5."""
    return math.log10(max(hours, 1))


def hours_since(now: float, then: float) -> int:
    return max(0, int((now - then) // 3600))  # whole hours, so points don't change every second


def ranker(name: str, spread: bool = False):
    """Register a ranker. With spread, the feed is built from the top down and each item loses
    repeat_per_post for every item from the same subscription above it (see rank)."""
    def register(fn: Ranker) -> Ranker:
        fn.spread = spread
        RANKERS[name] = fn
        return fn

    return register


@ranker("chronological")
def chronological(item: dict, now: float, weights: dict) -> Reasons:
    """No rules: every item scores 0, so the feed is simply newest first."""
    return []


@ranker("score", spread=True)
def score(item: dict, now: float, weights: dict) -> Reasons:
    """Points for the item's group (next up, unseen or seen) and its time in steps of ten, plus new
    subscription and green. rank() then takes points off repeats from the same subscription."""
    w = weights
    reasons = []
    if item.get("next_after") and "latest" not in item["tags"]:
        if w["next_up_bonus"]:
            reasons.append((f"next up after “{short(item['next_after'])}”", w["next_up_bonus"]))
    elif item["last_viewed"] is None:
        if w["unseen_bonus"]:
            reasons.append(("never opened", w["unseen_bonus"]))
        age = hours_since(now, item["date"])
        if steps(age) and w["age_per_step"]:
            label = f"{'added' if item['kind'] == 'link' else 'published'} {hours_label(age)} ago"
            reasons.append((label, points(w["age_per_step"] * steps(age))))
    else:
        if w["seen_bonus"]:
            reasons.append(("opened before", w["seen_bonus"]))
        away = hours_since(now, item["last_viewed"])
        if steps(away) and w["rediscovery_per_step"]:
            reasons.append((f"opened {hours_label(away)} ago", points(w["rediscovery_per_step"] * steps(away))))
    if item["kind"] == "subscription" and item.get("sub_last_viewed") is None and w["new_sub_bonus"]:
        reasons.append((f"nothing opened from “{short(item['feed'])}” yet", w["new_sub_bonus"]))
    if item["color"] == "green" and w["green_bonus_points"]:
        reasons.append(("green", w["green_bonus_points"]))
    elif item["color"] == "red":
        reasons.append(("red", -RED))
    return reasons


def rank(name: str, items: list[dict], now: float, weights: dict | None = None) -> list[dict]:
    """Return items sorted by score, each with "score" and "reasons" filled in. Weights other than the
    saved ones are for previewing them, such as while you move a slider.

    For a spread ranker the feed is built from the top down: the next item is always the one with
    the highest score after taking off repeat_per_post for each item from its subscription already
    placed above it, so the second post from a subscription loses repeat_per_post, the third twice that."""
    fn = RANKERS[name]
    weights = weights or WEIGHTS
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now, weights)]
        scored.append({**item, "score": points(sum(r["points"] for r in reasons)), "reasons": reasons})
    # On equal scores: things you haven't opened first, then newest first
    order = lambda it: (it["score"], it["last_viewed"] is None, it["date"], it["position"])
    penalty = weights.get("repeat_per_post", 0) if fn.spread else 0
    if not penalty:
        return sorted(scored, key=order, reverse=True)
    # Each subscription's items, best first; links are each on their own
    groups: dict[str, list[dict]] = {}
    for it in sorted(scored, key=order, reverse=True):
        groups.setdefault(it.get("sub") or it["url"], []).append(it)
    queues = list(groups.values())
    # Heap of each subscription's best item still to place, by score with its repeats taken off
    heap = [(-q[0]["score"], q[0]["last_viewed"] is not None, -q[0]["date"], -q[0]["position"], i, 0) for i, q in enumerate(queues)]
    heapq.heapify(heap)
    out = []
    while heap:
        *_, i, n = heapq.heappop(heap)
        it = queues[i][n]
        if n:
            label = f"{n} from “{short(it['feed'])}” higher up"
            it["reasons"].append({"label": label, "points": points(-penalty * n)})
            it["score"] = points(it["score"] - penalty * n)
        out.append(it)
        if n + 1 < len(queues[i]):
            nxt = queues[i][n + 1]
            heapq.heappush(heap, (-(nxt["score"] - penalty * (n + 1)), nxt["last_viewed"] is not None, -nxt["date"], -nxt["position"], i, n + 1))
    return out
