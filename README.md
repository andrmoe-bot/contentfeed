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

Everything personal is kept in the data directory (`--data-dir`, default `data/` next to
`server.py`), never in the code, and `data/` is git-ignored:

- `links.txt` and `feeds.txt`: your links and subscriptions (created with instructions on first run)
- `added.json`: when each link or post was first seen
- `viewed.json`: when you last opened each item
- `colors.json`: the items you marked green or red
- `settings.json`: your scoring settings from the Settings page, if you changed them
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
your comments in it.

You can also add links and subscriptions (with tags) from the box at the top of the feed; they
are appended to the files. To remove a link, edit `links.txt`. Changes to either file show up
within about 30 seconds.

Page details for links are fetched once; subscription posts are kept between checks. "Refresh all"
re-fetches links and checks every subscription now.

## Viewed items

Opening an item (clicking its link, middle-clicking it, or playing its video) records when you
last viewed it. Scrolling past doesn't count. Nothing is hidden: for 30 days after you open an
item, it ranks as if it were 100 days older (by default; see Ranking). The card says when you viewed it.

Cards you open or color stay where they are until you reload the page, so nothing moves away
while you're looking at it. Playing a video stops the one playing in another card.

## Colors

Each card has three dots in its bottom corner: green, white and red. They don't have proper names
yet; for now they change an item's score:

- **green**: +20, as if it were 20 days newer
- **white**: the default, no change. Clicking white also marks the item viewed: a quick "seen it".
- **red**: −100000, which puts it at the bottom, below everything else

These are the defaults; you can change them on the Settings page.

## Ranking

Feed order comes from a ranker in `ranking.py`. Rankers never learn from how you use the
feed; they only use what you wrote (tags, which file or subscription an item came from, the site,
the colors you picked), dates (when a post was published, or when a link was added) and when you
last opened an item. A ranker returns reasons with points, such as
`("tagged music", 2)`; the score is their sum. Click "Score" on any card to see its reasons.

The default, `score`, starts from the item's age and adjusts it, one point per day:

    score = −(age in days) + 20 if green + 10000 if next in its subscription
            − 100000 if red − 100 if opened in the last 30 days

"Next in its subscription" follows along with channels and playlists: in each subscription, the
post published right after the one you opened most recently gets the bonus. Open episode 4 and
episode 5 rises to the top, however old it is; open episode 5 and the bonus moves on to episode 6. This also works
for older YouTube videos, which are in the channel's upload order. If you most recently opened a
subscription's newest post, none of its posts get the bonus.

Change the numbers on the Settings page (`/settings`, linked at the top of the feed); the feed
uses them from its next update. The defaults are at the top of `ranking.py`. Equal scores are ordered newest first. The other ranker,
`chronological`, has no rules: everything scores 0 and the newest item is first. To add another ranker, register a
function with `@ranker("name")`, then select it with `python3 server.py --ranker name`, or try
it without a restart at `/api/feed?ranker=name`.
