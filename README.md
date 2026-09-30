# Content Feed

Turns a plain list of links into a browsable card feed (title, description, preview image,
inline YouTube embeds), served to your local network. Python 3 standard library only.

## Run

    python3 server.py            # binds 0.0.0.0:8090
    python3 server.py --port 9000

Open the printed `http://<lan-ip>:8090` address from any device on the network.

## Links

Edit `links.txt`: one URL per line, lines starting with `#` are comments. Links lower in the
file appear first in the feed. You can also add links with the box at the top of the page,
which appends them to `links.txt`. Changes to the file show up on the next page poll
(within about 30s).

Page metadata is fetched once per link and cached in `cache.json`; "Refresh all" re-fetches it.
