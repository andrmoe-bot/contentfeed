# Content Feed

Turns a plain list of links and feeds (RSS, Atom, YouTube channels, subreddits, blogs…) into a browsable card feed (title, description, preview image,
inline YouTube embeds), served to your local network. Python 3 standard library only.

## Run

    python3 server.py --port 8090         # development; keeps data in ./data
    python3 server.py --port 8090 --data-dir /some/other/dir

The default port is 80, so the service is reachable at plain `http://<lan-ip>`. Ports below
1024 need root or `CAP_NET_BIND_SERVICE`; the systemd service grants just that permission, so it
doesn't run as root. For development, pick a port of 1024 or above as shown, and open the
printed address from any device on the network.

## Your data

Everything personal is kept in the data directory, never in the code. Where that is depends on
how you run the feed:

- as the systemd service (see Run as a service): `/var/lib/contentfeed`
- with `python3 server.py`: `data/` next to `server.py` (git-ignored), or the folder given with `--data-dir`

It holds:

- `links.txt` and `feeds.txt`: your links and subscriptions (created with instructions on first run)
- `added.json`: when each link or post was first seen
- `viewed.json`: when you last opened each item
- `colors.json`: the items you marked green or red
- `settings.json`: your score settings from the feed's score panel, if you changed them
- `jellyfin.json`: logins for Jellyfin servers you subscribe to (see Jellyfin), if any
- `cache.json` and `feeds.json`: fetched page details and subscription posts

To back up or move your feed, copy this directory.

## Run as a service

`deploy/contentfeed.service` runs the feed with systemd, for example in its own LXC container or
VM. The code is a git checkout at `/opt/contentfeed` owned by root, which the service can only
read. The data lives in `/var/lib/contentfeed`, which systemd creates and which only the
`contentfeed` user can read. As root in the container (Debian/Ubuntu shown):

    apt install python3 git
    useradd --system --no-create-home --home-dir /var/lib/contentfeed --shell /usr/sbin/nologin contentfeed
    git clone https://github.com/andrmoe/contentfeed.git /opt/contentfeed
    cp /opt/contentfeed/deploy/contentfeed.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now contentfeed

The container needs an address on your LAN (e.g. a bridged network) for other devices to reach
port 80. Logs: `journalctl -u contentfeed -f`; they include the addresses the server fetches.

To update: `git -C /opt/contentfeed pull && systemctl restart contentfeed`. If the service
file changed, copy it to `/etc/systemd/system/` again and run `systemctl daemon-reload` first.

To edit your lists by hand, edit `/var/lib/contentfeed/links.txt` or `feeds.txt` as root; the
running server picks up changes within about 30 seconds.

If the service fails to start with `status=226/NAMESPACE`, the container doesn't allow systemd's
sandboxing; comment out the sandboxing block in the unit file.

## Links and subscriptions

In the data directory, `links.txt` holds single links and `feeds.txt` holds subscriptions. In both, each line is a URL
optionally followed by space-separated tags (`https://example.com music longread`), and lines
starting with `#` are comments.

A subscription can be an RSS or Atom feed, or a page that has one: a YouTube channel
(`youtube.com/@handle`, `/channel/…`) or playlist, a subreddit, a blog, a Mastodon profile and so on.
The server finds the feed, checks it every 30 minutes (`--feed-interval MINUTES`), and adds
its posts to the feed.

Posts stay in the feed after they drop out of the subscription's feed (up to 5000 per subscription).
Feeds only list recent posts (a YouTube channel's feed has its latest 15 videos), so for YouTube
channels and playlists the server also loads every older video once, right after subscribing. It
reads them from YouTube's playlist page, which needs no API key but gives upload dates only as
"3 years ago", so these videos show approximate dates ("about 3 years ago"). This uses YouTube's own
page data rather than an official API, so it can break when YouTube changes its pages; the
Subscriptions page then shows the error and the server tries again every 6 hours.

The feed shows 300 items at a time; "Show more" at the bottom loads the next 300. Newest come first,
so older videos are near the end.

The Subscriptions page (`/subscriptions`, linked at the top of the feed) lists every subscription
with its post count, latest post, last check and any errors. From there you can subscribe, edit
tags, check a feed now and unsubscribe. Every change is written straight to `feeds.txt`, keeping
your comments in it. Unsubscribing also forgets which of its posts you opened or colored (unless a post is
also in `links.txt` or another subscription), so subscribing again starts afresh. Removing a
subscription by editing `feeds.txt` keeps that history.

You can also add links and subscriptions (with tags) from the box at the top of the feed; they
are appended to the files. To remove a link, edit `links.txt`. Changes to either file show up
within about 30 seconds.

Page details for links are fetched once; subscription posts are kept between checks. "Refresh all"
re-fetches links and checks every subscription now.

## Jellyfin

You can subscribe to the movies and TV episodes on a [Jellyfin](https://jellyfin.org) server. First
add a login for the server to `jellyfin.json` in the data directory (`/var/lib/contentfeed` for the
service; create the file if it isn't there).
The key is the server's address, the same as in the browser up to `/web`:

    {"https://jellyfin.example": {"username": "me", "password": "…"}}

The address must match the one you subscribe to, including `http://` or `https://` and the port
(for example `http://jellyfin.home.arpa:8096`). With the systemd service the file is
`/var/lib/contentfeed/jellyfin.json`; if you create it as root, make it readable only by the service
with `chown contentfeed: /var/lib/contentfeed/jellyfin.json && chmod 600 /var/lib/contentfeed/jellyfin.json`.
If the feed can't use the file, subscribing says why.

The feed sees what that user sees. A Jellyfin user just for the feed, without admin rights and with only
the libraries you want, limits what the password stored here can do. Then subscribe to:

- the server's address, for every movie and episode on it, or
- a page on it, copied from the browser (`https://jellyfin.example/web/#/details?id=…`): a library,
  series, season or collection, for what's in it.

The server is checked like any other subscription, and the feed picks up changes to `jellyfin.json`
at the next check. Movies and episodes are dated by when they were added to the server ("added 2 days
ago"), so new additions come first, and ones removed from the server leave the feed.

Click a card's picture to play it right there; the title opens it in Jellyfin's web app instead.
The video comes through the feed server, which logs in for you, so your browser needs no Jellyfin
login and never sees the password or token. Jellyfin converts files the browser can't play as they are
(such as MKV or HEVC) to HLS, which Chromium-based browsers like Vivaldi play, but Firefox doesn't.
The feed server only passes on videos of posts in the feed, but anyone who can open the feed can play
them. Items without a picture have no play button, so the card just opens them in Jellyfin, and a video
that can't play in the card (for example if Jellyfin fails to convert it) shows a link to open it in
Jellyfin instead.

"Next in its subscription" goes by series: open an episode and the
next one in season and episode order gets the bonus. Movies don't get it. Whether you've watched
something in Jellyfin doesn't count as opened here; only opening it from the feed does.

## Viewed items

Opening an item (clicking its link, middle-clicking it, or playing its video) records when you
last viewed it. Scrolling past doesn't count. Nothing is hidden: an item you've opened loses its
head start and then climbs back slowly the longer you leave it and its subscription alone (see
Ranking). The card says when you viewed it.

Cards you open or color stay where they are until you reload the page, so nothing moves away
while you're looking at it. Playing a video stops the one playing in another card.

## Colors

Each card has three dots in its bottom corner: green, white and red. They don't have proper names
yet; for now they change an item's score:

- **green**: +100 by default; you can change it in the score panel (see Ranking)
- **white**: the default, no change. Clicking white also marks the item viewed: a quick "seen it".
- **red**: puts it at the bottom, below everything else

## Ranking

Feed order comes from a ranker in `ranking.py`. Rankers never learn from how you use the
feed; they only use what you wrote (tags, which file or subscription an item came from, the site,
the colors you picked), dates (when a post was published, or when a link was added) and when you
last opened an item. A ranker returns reasons with points, such as
`("tagged music", 2)`; the score is their sum. Click "Score" on any card to see its reasons.

The default, `score`, adds up three things, all counted in whole hours. With the default settings:

    score = 0.01 × (hours away)
            + (100 − hours since it became new to you)    head start, for things you haven't opened
            + 50 if it's the next post in a series
            + 100 if green
            − 100 × (posts from its subscription higher up in the feed)
            red items at the bottom

**Rediscovery.** Hours away are the hours since you opened the item or anything from its
subscription, whichever was later. Things you've left alone for a long time come back by
themselves: a channel you last watched a year ago gets 88 points, about as much as a post published
12 hours ago. Things you've never opened from a subscription you've never opened count from when
they came into the feed.

**Freshness.** New things start with a head start that loses one point per hour, so with the
default of 100 it's gone after about four days. Only items you haven't opened get one:

- a new post, counted from when it was published, and a new link from when you added it
- the newest post of a subscription you've never opened anything from, counted from when you
  subscribed, so a new subscription shows its latest post near the top (only that one)
**Next in series.** The post right after the one you opened most recently in its subscription gets
50 points: about as much as a post published two days ago, so after episode 4, episode 5 is
high in the feed but not on top. Open episode 5 and the bonus moves on to episode 6. This works for
channels, playlists and Jellyfin series (in episode order). Tag a subscription `latest` in
feeds.txt to skip this for it, such as a news feed.

**Mixing.** The feed is built from the top down: each place goes to the highest score after taking
100 points off for every post from the same subscription already placed above it, so the second post
from a subscription loses 100, the third 200, and so on. Two posts from one subscription in a row
are rare unless you have few subscriptions.

Each card's "Score" shows its points, such as
`opened something from “Veritasium” 2 days ago +0.48 · next after “Episode 4” +50`.

Change the numbers in the score panel: click "Score settings" at the top of the feed. It opens
beside the feed on a wide screen, or along the bottom on a narrow one, and stays open across reloads
until you close it. There are five settings, each with a slider and a box for an exact number:
Rediscovery (points per hour away), Freshness (head start), Next in series, Repeat penalty, and
Green. Moving one up gives you more of what it's named after. The sliders go in steps of 1, 1.5, 2, 3,
5 and 7 times a power of ten, so they cover both small and large numbers. The feed reorders as
you move a slider, and the setting is saved when you let go (or when you press Enter in the box). The defaults are at the top of `ranking.py`. Equal scores are ordered newest first. The other ranker,
`chronological`, has no rules: everything scores 0 and the newest item is first. To add another ranker, register a
function with `@ranker("name")`, then select it with `python3 server.py --ranker name`, or try
it without a restart at `/api/feed?ranker=name`.
