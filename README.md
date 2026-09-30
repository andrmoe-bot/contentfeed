# Content Feed

Turns a plain list of links and feeds (RSS, Atom, YouTube channels, subreddits, blogs…) into a browsable card feed (title, description, preview image,
inline YouTube embeds), served to your local network. Python 3 standard library only.

## Run

    python3 server.py            # binds 0.0.0.0:8090
    python3 server.py --port 9000

Open the printed `http://<lan-ip>:8090` address from any device on the network.

## Run as a service

`deploy/contentfeed.service` runs the feed with systemd, for example in its own LXC container or VM.
It expects the repository at `/opt/contentfeed`, owned by a `contentfeed` user. As root in the
container (Debian/Ubuntu shown):

    apt install python3 git
    useradd --system --home-dir /opt/contentfeed --shell /usr/sbin/nologin contentfeed
    git clone https://github.com/andrmoe/contentfeed.git /opt/contentfeed
    chown -R contentfeed:contentfeed /opt/contentfeed
    cp /opt/contentfeed/deploy/contentfeed.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now contentfeed

To bring your existing lists and history along, stop the service, copy `links.txt`, `feeds.txt`,
`added.json`, `cache.json` and `feeds.json` into `/opt/contentfeed` (owned by `contentfeed`),
then start it again.

The container needs an address on your LAN (e.g. a bridged network) for other devices to reach
port 8090. Logs: `journalctl -u contentfeed -f`. To update:
`sudo -u contentfeed git -C /opt/contentfeed pull && systemctl restart contentfeed`.

If the service fails to start with `status=226/NAMESPACE`, the container doesn't allow systemd's
sandboxing; comment out the sandboxing block in the unit file.

## Links and subscriptions

`links.txt` holds single links and `feeds.txt` holds subscriptions. In both, each line is a URL
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

Page details for links are fetched once and cached in `cache.json`; subscription posts are cached
in `feeds.json`. "Refresh all" re-fetches links and checks every subscription now.

## Ranking

Feed order comes from a ranker in `ranking.py`. Rankers never learn from how you use the
feed; they only use what you wrote (tags, which file or subscription an item came from, the site)
and dates (when a post was published, or when a link was added, stored in `added.json`). A ranker returns reasons with points, such as
`("tagged music", 2)`; the score is their sum. Click "Score" on any card to see its reasons.

The default, `chronological`, has no rules: everything scores 0 and the newest item is first. To add another ranker, register a
function with `@ranker("name")`, then select it with `python3 server.py --ranker name`, or try
it without a restart at `/api/feed?ranker=name`.
