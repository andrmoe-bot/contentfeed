"""Feed ranking.

Rankers are deliberately simple and explainable: they never learn from how the feed is used.
A ranker looks at one item and returns its reasons, each a (label, points) pair, such as
("tagged music", 2). An item's score is the sum of its points, the feed is sorted by score
(highest first, newest date first on ties), and every card shows its reasons on the page.

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
    sub_last_viewed  for a post: unix time you last opened any post in its subscription, or None
                 if you've never opened anything from it

Nothing is hidden: items you've opened or marked red are scored differently, not removed.

Register a ranker with @ranker("name") and select it with `server.py --ranker name`, or try it
without restarting via /api/feed?ranker=name.

Keep points stable between page updates, for example by using whole days rather than seconds
for age, so the page doesn't redraw every time it checks for changes.
"""

from typing import Callable

Reasons = list[tuple[str, float]]
Ranker = Callable[[dict, float], Reasons]
RANKERS: dict[str, Ranker] = {}
DEFAULT = "score"

# Settings for the "score" ranker. Age, days since you last opened an item and days since you last
# opened anything in its subscription are multiplied by their weights; the rest are points added or taken away. These are the defaults; you can change
# them on the Settings page (/settings), and server.py keeps your values in settings.json.
DEFAULT_WEIGHTS = {
    "age_weight": -1,  # points per day since the item was published (or added)
    "seen_weight": 2,  # points per day since you last opened it; items you never opened get never_seen_bonus
    "sub_seen_weight": 10,  # points per day since you last opened any post in the item's subscription
    "never_seen_bonus": 1000,
    "sub_never_seen_bonus": 1000,  # for posts from subscriptions you've never opened anything from
    "green_bonus": 20,
    "red_penalty": 100000,  # red items sink below everything else
    "next_bonus": 10000,  # the post after the one you last opened in a subscription rises above anything not red
}
WEIGHTS = dict(DEFAULT_WEIGHTS)
SIGNED = {"age_weight", "seen_weight", "sub_seen_weight"}  # may be negative; the others are amounts added or taken away
LIMIT = 10_000_000


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


def days_label(days: int) -> str:
    return f"{days} day{'s' if days != 1 else ''}"


def short(title: str) -> str:
    return title if len(title) <= 60 else title[:59] + "…"


def points(x: float) -> float:
    return int(x) if float(x).is_integer() else round(x, 2)


def ranker(name: str):
    def register(fn: Ranker) -> Ranker:
        RANKERS[name] = fn
        return fn

    return register


@ranker("chronological")
def chronological(item: dict, now: float) -> Reasons:
    """No rules: every item scores 0, so the feed is simply newest first."""
    return []


@ranker("score")
def score(item: dict, now: float) -> Reasons:
    """Weighted age, days since you last opened the item and days since you last opened anything in
    its subscription, adjusted by color and by whether it's next in a subscription you're following along."""
    w = WEIGHTS
    days = int((now - item["date"]) // 86400)  # whole days, so points don't change every second
    reasons = [(f"{days_label(days)} old", points(w["age_weight"] * days))] if days > 0 and w["age_weight"] else []
    if item["last_viewed"] is None:
        if w["never_seen_bonus"]:
            reasons.append(("never opened", w["never_seen_bonus"]))
    else:
        seen = int((now - item["last_viewed"]) // 86400)
        if seen > 0 and w["seen_weight"]:
            reasons.append((f"opened {days_label(seen)} ago", points(w["seen_weight"] * seen)))
    if item["kind"] == "subscription" and item.get("sub_last_viewed") is None:
        if w["sub_never_seen_bonus"]:
            reasons.append((f"nothing opened from “{short(item['feed'])}”", w["sub_never_seen_bonus"]))
    elif item.get("sub_last_viewed") is not None:
        sub_seen = int((now - item["sub_last_viewed"]) // 86400)
        if sub_seen > 0 and w["sub_seen_weight"]:
            label = f"opened something from “{short(item['feed'])}” {days_label(sub_seen)} ago"
            reasons.append((label, points(w["sub_seen_weight"] * sub_seen)))
    if item["color"] == "green" and w["green_bonus"]:
        reasons.append(("green", w["green_bonus"]))
    elif item["color"] == "red" and w["red_penalty"]:
        reasons.append(("red", -w["red_penalty"]))
    if item.get("next_after") and w["next_bonus"]:
        reasons.append((f"next after “{short(item['next_after'])}”", w["next_bonus"]))
    return reasons


def rank(name: str, items: list[dict], now: float) -> list[dict]:
    """Return items sorted by score, each with "score" and "reasons" filled in."""
    fn = RANKERS[name]
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now)]
        scored.append({**item, "score": sum(r["points"] for r in reasons), "reasons": reasons})
    return sorted(scored, key=lambda it: (it["score"], it["date"], it["position"]), reverse=True)
