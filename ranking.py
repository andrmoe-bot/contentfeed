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
                 if this post comes right after that one ("what's next"): the next part of its series
                 (see title_series) or else the next published; otherwise None
    series_first for a later part of a series of which you haven't opened anything before it: the
                 first part's title; otherwise None
    sub          for a post: its subscription's address; None for links
    sub_last_viewed  for a post: unix time you last opened any post in its subscription, or None
                 if you've never opened anything from it

rank() adds one more, "similar": the item you opened recently that this one is most like (see
similarity), or None.

Nothing is hidden: items you've opened or marked red are scored differently, not removed.

A ranker registered with spread=True gets its repeats spread out after scoring (see rank).

Register a ranker with @ranker("name") and select it with `server.py --ranker name`, or try it
without restarting via /api/feed?ranker=name.

Keep points stable between page updates, for example by using whole hours rather than seconds
for age, so the page doesn't redraw every time it checks for changes.
"""

import heapq
import re
from typing import Callable

Reasons = list[tuple[str, float]]
Ranker = Callable[[dict, float, dict], Reasons]
RANKERS: dict[str, Ranker] = {}
DEFAULT = "score"

# Settings for the "score" ranker, in points. These are the defaults; you can change them in the feed's score panel, and server.py keeps your
# values in settings.json.
DEFAULT_WEIGHTS = {
    "next_up_bonus": 30,  # the post after the one you opened most recently in a subscription
    "seen_bonus": -30,  # items you've opened (other than next up); below 0 puts them under unopened ones
    "new_sub_bonus": 20,  # for posts from a subscription you've never opened anything from
    "similar_penalty": 40,  # taken off at 100% like something you opened recently (see similarity):
    "similar_hours": 24,  # opened within this many hours
    "similar_days": 30,  # posts from the same subscription are alike if published within this many days
    "tag_similarity": 30,  # percent alike per shared tag, for items from different subscriptions
    "series_penalty": 30,  # taken off a later part of a series when you haven't opened anything before it
    "repeat_per_post": 15,  # taken off for each post from the same subscription higher up in the feed
    "green_bonus_points": 10,
}
WEIGHTS = dict(DEFAULT_WEIGHTS)
SIGNED = {"seen_bonus"}  # may be negative
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


def hours_since(now: float, then: float) -> int:
    return max(0, int((now - then) // 3600))  # whole hours, so points don't change every second


# Similarity: how alike an item is to one you opened, from 0 to 100%. Two posts from the same
# subscription are 100% alike if published at the same time, falling evenly to 0% when published
# similar_days or more apart. Items from different subscriptions (or links) are tag_similarity alike
# for each tag they share, other than "latest". At most 100%. An item is 100% like itself, so one you
# just opened loses the whole penalty too.
def similarity(item: dict, other: dict, weights: dict) -> tuple[int, str]:
    """How alike the two items are, in whole percent, and why, such as "same subscription, published 4 days apart"."""
    if item["url"] == other["url"]:
        return 100, "itself"
    if item.get("sub") and item.get("sub") == other.get("sub"):
        gap = abs(item["date"] - other["date"]) / 3600
        days = weights["similar_days"]
        percent = max(0.0, 1 - gap / 24 / days) if days else 0.0
        apart = f"published {hours_label(int(gap))} apart" if gap >= 1 else "published within the hour"
        return round(100 * percent), f"same subscription, {apart}"
    shared = sorted(set(item["tags"]) & set(other["tags"]) - {"latest"})
    percent = min(100, weights["tag_similarity"] * len(shared))
    return round(percent), f"tag{'s' if len(shared) > 1 else ''} {', '.join(shared)}"


def similar_to_recent(items: list[dict], now: float, weights: dict):
    """Set each item's "similar" to the item opened in the last similar_hours that it's most like, as
    {title, hours (since opened), percent, why}, or None if it's 0% like all of them. An item opened in
    that time is 100% like itself."""
    recent = [it for it in items if it["last_viewed"] is not None and hours_since(now, it["last_viewed"]) < weights["similar_hours"]]
    for it in items:
        best = None
        if weights["similar_penalty"]:
            for other in recent:
                percent, why = similarity(it, other, weights)
                # On a tie, itself is the clearest reason
                if percent and (not best or percent > best["percent"] or (percent == best["percent"] and why == "itself")):
                    best = {"title": other["title"], "hours": hours_since(now, other["last_viewed"]), "percent": percent, "why": why}
        it["similar"] = best


# Series: a post follows another from the same subscription when their titles have the same numbers
# except one, which is 1 higher, such as "Making a CPU, part 2" after "Making a CPU, part 1", or
# "S01E05" after "S01E04". Only the numbers are compared, not the words around them, and only between
# posts at most SERIES_NEAR apart in the subscription. Numbers of 1000 or more, such as years, don't count.
SERIES_NEAR = 10


def title_numbers(title: str) -> tuple[int, ...]:
    return tuple(n for n in map(int, re.findall(r"\d+", title)) if n < 1000)


def follows(before: tuple[int, ...], after: tuple[int, ...]) -> bool:
    """Whether numbers after come right after before: all the same but one, which is 1 higher."""
    return len(before) == len(after) and sum(a != b for a, b in zip(before, after)) == 1 and sum(after) == sum(before) + 1


def title_series(posts: list[dict]) -> dict[str, dict]:
    """For a subscription's posts, in feed order: {a post's url: the post it follows}, the nearest one if several."""
    numbers = [title_numbers(p["title"] or "") for p in posts]
    out = {}
    for i, p in enumerate(posts):
        if not numbers[i]:
            continue
        near = sorted(range(max(0, i - SERIES_NEAR), min(len(posts), i + SERIES_NEAR + 1)), key=lambda j: abs(j - i))
        for j in near:
            if follows(numbers[j], numbers[i]):
                out[p["url"]] = posts[j]
                break
    return out


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
    """Points for next up or opened before, a new subscription, a later part of a series, being like
    something opened recently, and green. rank() then takes points off repeats from the same subscription."""
    w = weights
    reasons = []
    if item.get("next_after") and "latest" not in item["tags"]:
        if w["next_up_bonus"]:
            reasons.append((f"next up after “{short(item['next_after'])}”", w["next_up_bonus"]))
    elif item["last_viewed"] is not None and w["seen_bonus"]:
        away = hours_since(now, item["last_viewed"])
        reasons.append((f"opened {hours_label(away)} ago" if away else "opened in the last hour", w["seen_bonus"]))
    if item["kind"] == "subscription" and item.get("sub_last_viewed") is None and w["new_sub_bonus"]:
        reasons.append((f"nothing opened from “{short(item['feed'])}” yet", w["new_sub_bonus"]))
    if item.get("series_first") and "latest" not in item["tags"] and w["series_penalty"]:
        reasons.append((f"in a series starting “{short(item['series_first'])}”, nothing before it opened", -w["series_penalty"]))
    like = item.get("similar")
    if like and w["similar_penalty"]:
        when = f"{hours_label(like['hours'])} ago" if like["hours"] else "in the last hour"
        if like["why"] == "itself":
            label = f"100% like itself, opened {when}"
        else:
            label = f"{like['percent']}% like “{short(like['title'])}”, opened {when} ({like['why']})"
        reasons.append((label, points(-w["similar_penalty"] * like["percent"] / 100)))
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
    items = [dict(it) for it in items]
    similar_to_recent(items, now, weights)
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
