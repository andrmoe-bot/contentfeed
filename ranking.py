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

# Points for the "score" ranker. One point is one day of age.
GREEN = 20  # green items count as 20 days newer
RED = -100000  # red items sink below everything else (about 270 years)
VIEWED = -100  # opened in the last VIEWED_DAYS days: counts as 100 days older
VIEWED_DAYS = 30


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
    if item["color"] == "green":
        reasons.append(("green", GREEN))
    elif item["color"] == "red":
        reasons.append(("red", RED))
    if item["last_viewed"] and now - item["last_viewed"] < VIEWED_DAYS * 86400:
        reasons.append((f"opened in the last {VIEWED_DAYS} days", VIEWED))
    return reasons


def rank(name: str, items: list[dict], now: float) -> list[dict]:
    """Return items sorted by score, each with "score" and "reasons" filled in."""
    fn = RANKERS[name]
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now)]
        scored.append({**item, "score": sum(r["points"] for r in reasons), "reasons": reasons})
    return sorted(scored, key=lambda it: (it["score"], it["date"], it["position"]), reverse=True)
