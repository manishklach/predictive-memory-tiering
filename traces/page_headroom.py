"""Screen page selectors on a timestamped VFS trace without changing residency."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path


def evaluate(path):
    with path.open(newline="") as stream:
        events = [(int(row["timestamp_ns"]), int(row["page"]))
                  for row in csv.DictReader(stream)]
    times = [t for t, _ in events]
    if times != sorted(times):
        raise ValueError("timestamps must be monotone")
    positions = defaultdict(list)
    for i, (_, page) in enumerate(events):
        positions[page].append(i)
    split = len(events) // 2
    result = {}
    for horizon in ("100ms", "64_reads"):
        history = defaultdict(Counter)
        totals = Counter()
        seen = {}
        counts = Counter()
        for i, (now, page) in enumerate(events):
            seen[page] = now
            counts[page] += 1
            if i % 64:
                continue
            end = (bisect_right(times, now + 100_000_000) - 1
                   if horizon == "100ms" else i + 64)
            if end >= len(events) or (horizon == "100ms" and now + 100_000_000 > times[-1]):
                continue
            if i < split <= end:
                continue
            regions = defaultdict(list)
            for p, last in seen.items():
                if now - last >= 100_000_000:
                    regions[p // 32].append(p)
            eligible = [pages for pages in regions.values() if len(pages) >= 17]
            if not eligible:
                continue
            pages = min(eligible, key=lambda group: (max(seen[p] for p in group), min(group)))
            features = {p: (((now - seen[p]) // 100_000_000).bit_length(),
                            counts[p].bit_length()) for p in pages}
            reused = {p: bisect_right(positions[p], end) > bisect_right(positions[p], i)
                      for p in pages}
            if i < split:
                for p in pages:
                    bucket = history[features[p]]
                    bucket["samples"] += 1
                    bucket["reuse"] += reused[p]
                continue

            def risk(p):
                bucket = history.get(features[p], {})
                return ((1 + bucket.get("reuse", 0)) / (2 + bucket.get("samples", 0)),
                        seen[p], p)

            orders = {"address": sorted(pages), "lru": sorted(pages, key=lambda p: (seen[p], p)),
                      "learned": sorted(pages, key=risk), "oracle": sorted(pages, key=reused.get)}
            totals["epochs"] += 1
            totals["selected_per_policy"] += 16
            for policy, order in orders.items():
                totals[policy] += sum(reused[p] for p in order[:16])
        result[horizon] = dict(totals)
    return {"trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "events": len(events), "split_event": split, "held_out_counts": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(evaluate(args.trace), indent=2) + "\n")
