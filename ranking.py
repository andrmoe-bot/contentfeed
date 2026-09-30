"""Feed ranking.

Rankers are deliberately simple and explainable: they never learn from how the feed is used.
A ranker looks at one item and returns its reasons, each a (label, points) pair, such as
("tagged music", 2). An item's score is the sum of its points, the feed is sorted by score
(highest first, newest date first on ties), and every card shows its reasons on the page.

Each item has its page metadata (url, title, description, domain, ...) plus these inputs:

    kind         "link" (from links.txt) or "subscription" (a post from a feed in feeds.txt)
    date         unix time: when a link was added, or when a post was published
    added        unix time the server first saw the link or post
    tags         words written after the URL on its line in links.txt or feeds.txt, lowercased
    feed         the subscription's title (posts only)
    position     index in links.txt (0 = first line); -1 for posts
    last_viewed  unix time you last opened it, or None

Hiding items you opened recently (server.py --hide-viewed) is separate from ranking: hidden
items are ranked like the rest but only shown with "Show viewed" turned on.

Register a ranker with @ranker("name") and select it with `server.py --ranker name`, or try it
without restarting via /api/feed?ranker=name.

Keep points stable between page updates, for example by using whole days rather than seconds
for age, so the page doesn't redraw every time it checks for changes.
"""

from typing import Callable

Reasons = list[tuple[str, float]]
Ranker = Callable[[dict, float], Reasons]
RANKERS: dict[str, Ranker] = {}
DEFAULT = "chronological"


def ranker(name: str):
    def register(fn: Ranker) -> Ranker:
        RANKERS[name] = fn
        return fn

    return register


@ranker("chronological")
def chronological(item: dict, now: float) -> Reasons:
    """No rules: every item scores 0, so the feed is simply newest first."""
    return []


def rank(name: str, items: list[dict], now: float) -> list[dict]:
    """Return items sorted by score, each with "score" and "reasons" filled in."""
    fn = RANKERS[name]
    scored = []
    for item in items:
        reasons = [{"label": label, "points": points} for label, points in fn(item, now)]
        scored.append({**item, "score": sum(r["points"] for r in reasons), "reasons": reasons})
    return sorted(scored, key=lambda it: (it["score"], it["date"], it["position"]), reverse=True)
