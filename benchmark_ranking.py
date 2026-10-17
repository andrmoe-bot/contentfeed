"""Reproducible ranking benchmark; no network or personal data.

python3 benchmark_ranking.py --baseline /tmp/old-ranking.py
Use `git show <old-commit>:ranking.py > /tmp/old-ranking.py` for a baseline.
"""

import argparse
import gc
import importlib.util
import random
import statistics
import time

import ranking

NOW = 2_000_000_000


def fixture(count=10_000, subscriptions=50, span_days=365, recent=True):
    rng = random.Random(831)
    items = []
    for i in range(count):
        sub = f'sub-{i % subscriptions}'
        items.append({
            'url': f'https://example.com/{i}', 'domain': 'example.com', 'title': f'Post {i}', 'sub': sub,
            'feed': sub, 'kind': 'subscription', 'tags': [], 'position': -1,
            'date': NOW - rng.random() * span_days * 86400,
            'last_viewed': NOW - rng.randrange(24) * 3600 if recent and i < 2 * subscriptions else None,
            'color': rng.choices(['white', 'green', 'red'], weights=[97, 2, 1])[0],
            'estimated_minutes': rng.choice([None, 5, 10, 20, 30, 60, 120]),
        })
    return items


def weights(module, mode):
    values = dict(module.DEFAULT_WEIGHTS, green_bonus_points=20, length_per_minute=-0.1)
    values['similar_length_weight'] = 1 if mode in ('mixed', 'length', 'all') else 0
    values['similar_publish_weight'] = 0 if mode in ('length', 'title') else 1
    if 'similar_title_weight' in values:
        values['similar_title_weight'] = 1 if mode in ('title', 'all') else 0
    if mode == 'disabled':
        values['above_similarity_penalty' if 'above_similarity_penalty' in values else 'repeat_per_post'] = 0
    return values


def measure(module, items, mode, runs, first_page=False):
    settings = weights(module, mode)
    module.rank('score', items, NOW, settings)  # warm caches/imports
    samples = []
    for _ in range(runs):
        gc.collect()
        start = time.perf_counter()
        if first_page:
            ranked = module.rank('score', items, NOW, settings, lazy=True)
            result, _ = ranked.read(300)
        else:
            result = module.rank('score', items, NOW, settings)
        samples.append(time.perf_counter() - start)
        expected = min(300, len(items)) if first_page else len(items)
        assert len(result) == expected
        assert len({it['url'] for it in result}) == expected
    return statistics.median(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline')
    parser.add_argument('--items', type=int, default=10_000)
    parser.add_argument('--subs', type=int, default=50)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--cases', nargs='+', help='run only named cases, such as publish mixed title all')
    args = parser.parse_args()
    baseline = None
    if args.baseline:
        spec = importlib.util.spec_from_file_location('baseline_ranking', args.baseline)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
    print(f'{args.items} items, {args.subs} subscriptions, median of {args.runs} runs (seconds)')
    print('case                  old full  old page  new full  new page  page/old')
    for mode, span, recent in [('publish', 365, True), ('publish-dense', 7, True),
                               ('mixed', 365, True), ('length', 365, True),
                               ('publish-no-views', 365, False), ('disabled', 365, True),
                               ('title', 365, True), ('all', 365, True)]:
        if args.cases and mode not in args.cases:
            continue
        items = fixture(args.items, args.subs, span, recent)
        seconds = measure(ranking, items, mode, args.runs)
        page = measure(ranking, items, mode, args.runs, first_page=True)
        if baseline and (mode not in ('title', 'all') or 'similar_title_weight' in baseline.DEFAULT_WEIGHTS):
            old = measure(baseline, items, mode, args.runs)
            old_page = measure(baseline, items, mode, args.runs, first_page=True) if hasattr(baseline, 'RankedItems') else old
            print(f'{mode:22s} {old:8.4f}  {old_page:8.4f}  {seconds:8.4f}  {page:8.4f}  {page / old_page:8.2f}x')
        else:
            print(f'{mode:22s} {"—":>8s}  {"—":>8s}  {seconds:8.4f}  {page:8.4f}')


if __name__ == '__main__':
    main()
