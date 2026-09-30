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

The Subscriptions page (`/subscriptions`, linked at the top of the feed) lists every subscription
with its post count, latest post, last check and any error. From there you can subscribe, edit
tags, check a feed now and unsubscribe. Every change is written straight to `feeds.txt`, keeping
your comments in it.

You can also add links and subscriptions (with tags) from the box at the top of the feed; they
are appended to the files. To remove a link, edit `links.txt`. Changes to either file show up
within about 30 seconds.

Page details for links are fetched once; subscription posts are kept between checks. "Refresh all"
re-fetches links and checks every subscription now.

## Viewed items

Opening an item (clicking its link, middle-clicking it, or playing its video) records when you
last viewed it. Scrolling past doesn't count. After a reload, viewed items are hidden for 30 days,
then they come back marked "shown again". Change the time with `--hide-viewed`: e.g. `12h`, `7d`,
`2w`, `never` (stay hidden until unmarked) or `0` (don't hide). For the service, add the option
to `ExecStart` in the unit file.

What you open stays on the page until you open something else, which hides it straight away (a
playing video stops). The browser's Back button brings it back, and Forward hides it again; this
keeps working after a reload in the same tab. "Show viewed" at the top brings all hidden items
back, each saying when you viewed it and when it returns, with an "Unmark" button to forget that
you viewed it.

## Ranking

Feed order comes from a ranker in `ranking.py`. Rankers never learn from how you use the
feed; they only use what you wrote (tags, which file or subscription an item came from, the site)
and dates (when a post was published, or when a link was added). A ranker returns reasons with points, such as
`("tagged music", 2)`; the score is their sum. Click "Score" on any card to see its reasons.

The default, `chronological`, has no rules: everything scores 0 and the newest item is first. To add another ranker, register a
function with `@ranker("name")`, then select it with `python3 server.py --ranker name`, or try
it without a restart at `/api/feed?ranker=name`.
