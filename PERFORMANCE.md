Ranking performance: similarity to posts above
=============================================

Measured locally with Python 3.11.2, 50 subscriptions and 10,000 items (200 per subscription),
using the median of five measured runs after a warm-up. The seeded fixture contains varied
lengths, some unknown lengths, colored posts and 100 recently viewed items (except the
no-views case). No network, HTTP serialization or background enrichment time is included.

| Scenario | Previous full ranking | New full ranking | New first 300 | Page / previous |
|---|---:|---:|---:|---:|
| Publish time, dates over a year | 239.5 ms | 657.1 ms | 143.7 ms | 0.60× |
| Publish time, dates within a week | 318.4 ms | 1704.0 ms | 246.2 ms | 0.77× |
| Publish time + length | 344.9 ms | 1940.0 ms | 259.4 ms | 0.75× |
| Length only | 329.9 ms | 1475.3 ms | 211.1 ms | 0.64× |
| Publish time, no recent views | 156.7 ms | 639.4 ms | 93.8 ms | 0.60× |
| Above-feed penalty disabled | 204.9 ms | 105.7 ms | 113.3 ms | 0.55× |

The previous server always ranked all items before returning a page. The new server returns
an exact ranking prefix and caches the iterator so “Show more” resumes it. Initial page
ranking was 23–45% faster in these fixtures. These are measurements on this machine, not
portable timing assertions.

The complete ranking has a real regression: comparing similarities costs more than counting
posts. A full pass can visit each same-subscription pair, O(sum of subscription sizes squared),
while memory remains O(items). The new default page avoids computing the unrequested tail.
A page-load metadata search may need a longer prefix when its highest eligible incomplete
posts rank below the visible page. When none are eligible, it does not scan the tail.

Each selection updates only its own subscription's remaining items; a group heap allows a
less-similar candidate to overtake the previous leader. Only group leaders enter the global
heap. Publish-only comparisons outside the date window are skipped; no pair matrix or
per-pair explanation strings are allocated. Recent-view explanations are built only for the
winning match, and the existing cached domain avoids reparsing each URL for media type.

Regression tests compare the result against an independent exhaustive greedy reference,
including randomized mixtures of weights, scores, missing lengths and ties. At the requested
scale, the publish-only fixture makes 144,000 numeric comparisons instead of roughly 50
million cross-subscription comparisons. The mixed-similarity first page makes fewer than
60,000 comparisons instead of eagerly processing about 1 million within-subscription pairs.
Tests also verify exact incremental order, stable prior pages, concurrent extensions,
metadata searches beyond the visible prefix, cache reuse and invalidation, and migration of
the old repeat setting.

To reproduce against the commit immediately before this change:

```sh
git show 2313eb1:ranking.py > /tmp/contentfeed-ranking-before-above.py
python3 benchmark_ranking.py --baseline /tmp/contentfeed-ranking-before-above.py --runs 5
```

Use `--items`, `--subs` and `--runs` to change the fixture size or repetition count. The
benchmark also runs without a baseline file and uses only the standard library.
