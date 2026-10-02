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

Nothing is hidden: items you've opened or marked red are scored down, not removed.

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

# Settings for the "score" ranker, where one point is one day of age. These are the defaults; you
# can change them on the Settings page (/settings), and server.py keeps your values in settings.json.
DEFAULT_WEIGHTS = {
    "green_bonus": 20,  # green items count as 20 days newer
    "red_penalty": 100000,  # red items sink below everything else (about 270 years)
    "viewed_penalty": 100,  # items opened in the last viewed_days days count as 100 days older
    "viewed_days": 30,
}
WEIGHTS = dict(DEFAULT_WEIGHTS)


def check_weights(weights: dict) -> dict:
    """The weights as numbers, or ValueError if one is missing, unknown, negative or too big."""
    if set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError(f"expected exactly these settings: {', '.join(DEFAULT_WEIGHTS)}")
    out = {}
    for key, value in weights.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10_000_000:
            raise ValueError(f"{key} must be a number from 0 to 10000000")
        out[key] = int(value) if float(value).is_integer() else value
    return out


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
    """Newest first, adjusted by color and by whether you opened the item recently."""
    days = int((now - item["date"]) // 86400)  # whole days, so points don't change every second
    reasons = [(f"{days} day{'s' if days != 1 else ''} old", -days)] if days > 0 else []
    w = WEIGHTS
    if item["color"] == "green" and w["green_bonus"]:
        reasons.append(("green", w["green_bonus"]))
    elif item["color"] == "red" and w["red_penalty"]:
        reasons.append(("red", -w["red_penalty"]))
    days_viewed = w["viewed_days"]
    if item["last_viewed"] and w["viewed_penalty"] and now - item["last_viewed"] < days_viewed * 86400:
        reasons.append((f"opened in the last {days_viewed:g} day{'s' if days_viewed != 1 else ''}", -w["viewed_penalty"]))
    return reasons


def rank(name: str, items: list[dict], now: float) -> list[dict]:
    """Return items sorted by score, each with "score" and "reasons" filled in."""
    fn = RANKERS[name]
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now)]
        scored.append({**item, "score": sum(r["points"] for r in reasons), "reasons": reasons})
    return sorted(scored, key=lambda it: (it["score"], it["date"], it["position"]), reverse=True)
