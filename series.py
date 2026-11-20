"""Series within a subscription: which posts are parts of the same series, and in what order.

YouTube series are imported channel playlists, in playlist order. Jellyfin and NRK TV posts carry their
series and episode numbers. Other posts form series by their titles (see ranking.title_series).
YouTube videos in no playlist, and posts marked no_series such as NRK TV's news, aren't in a series.
"""

import ranking

PLAYLIST_URL = "https://www.youtube.com/playlist?list="


def _chained(kind: str, ident: str, title: str, url: str | None, posts: list[dict]) -> dict:
    """A group whose parts follow each other in list order, last part first."""
    return {"kind": kind, "id": ident, "title": title, "url": url, "posts": posts,
            "prev": {p["url"]: posts[i + 1] for i, p in enumerate(posts[:-1])}}


def groups(subscription: dict) -> list[dict]:
    """A subscription's posts in groups, each {"kind", "id", "title", "url", "posts", "prev"}: its posts
    last part first (or newest first), and prev, which maps a post's url to the part before it. kind is
    "playlist", "series" (Jellyfin and NRK TV episode order), "titles" (other posts; prev comes from
    their titles, so it holds several series or none) or "loose" (posts in no series; prev is empty)."""
    items = subscription.get("items", [])
    out = []
    by_video = {p["youtube"]: p for p in items if p.get("youtube")}
    assigned = set()
    for playlist in subscription.get("youtube_playlists", []):
        ordered = [by_video[v] for v in playlist["videos"] if v in by_video]
        if ordered:
            out.append(_chained("playlist", playlist["id"], playlist.get("title") or f"YouTube playlist · {playlist['id']}",
                                PLAYLIST_URL + playlist["id"], list(reversed(ordered))))
            assigned.update(p["youtube"] for p in ordered)
    by_series, titled, loose_videos, unnumbered = {}, [], [], []
    for p in items:
        if p.get("youtube"):
            if p["youtube"] not in assigned:
                loose_videos.append(p)
        elif p.get("no_series"):
            unnumbered.append(p)
        elif p.get("series"):
            by_series.setdefault(p["series"], []).append(p)
        else:
            titled.append(p)
    for ident, posts in by_series.items():
        posts.sort(key=lambda p: p.get("episode", []), reverse=True)  # last episode first; stable otherwise
        out.append(_chained("series", str(ident), subscription.get("title") or "Series", None, posts))
    if titled:
        out.append({"kind": "titles", "id": None, "title": None, "url": None, "posts": titled,
                    "prev": ranking.title_series(titled)})
    for posts in (loose_videos, unnumbered):
        if posts:
            out.append({"kind": "loose", "id": None, "title": None, "url": None, "posts": posts, "prev": {}})
    return out


def chain(url: str, prev: dict[str, dict]) -> set[str]:
    """The urls of every part of the series url is in, by following prev both ways."""
    members = {url}
    while True:
        expanded = (members | {p for p, before in prev.items() if before["url"] in members}
                    | {before["url"] for p, before in prev.items() if p in members})
        if expanded == members:
            return members
        members = expanded


def of_post(subscription: dict, url: str) -> list[dict]:
    """The series the post at url is in, each {"id", "title", "url", "posts"}, with its posts first part first."""
    found = []
    for group in groups(subscription):
        if group["kind"] == "loose" or not any(p["url"] == url for p in group["posts"]):
            continue
        if group["kind"] == "titles":
            members = chain(url, group["prev"])
            if len(members) < 2:
                continue
            ordered = sorted((p for p in group["posts"] if p["url"] in members),
                             key=lambda p: (ranking.title_numbers(p.get("title") or ""), p.get("published") or 0))
            found.append({"id": "titles:" + min(members), "title": ordered[0].get("title") or "Series",
                          "url": None, "posts": ordered})
        else:
            found.append({"id": f"{group['kind']}:{group['id']}", "title": group["title"], "url": group["url"],
                          "posts": list(reversed(group["posts"]))})
    return found


def memberships(subscription: dict) -> dict[str, list[str]]:
    """{a post's url: the ids of the series it's in}, with the same ids as of_post, for posts in any series."""
    out = {}
    for group in groups(subscription):
        if group["kind"] == "loose":
            continue
        if group["kind"] == "titles":
            # Each set of posts that follow each other is a series, named by its smallest url.
            root = {}
            def find(url):
                while url in root:
                    url = root[url]
                return url
            for url, before in group["prev"].items():
                a, b = find(url), find(before["url"])
                if a != b:
                    root[max(a, b)] = min(a, b)
            for url in group["prev"].keys() | {before["url"] for before in group["prev"].values()}:
                out.setdefault(url, []).append("titles:" + find(url))
        else:
            for p in group["posts"]:
                out.setdefault(p["url"], []).append(f"{group['kind']}:{group['id']}")
    return out
