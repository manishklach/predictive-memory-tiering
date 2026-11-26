"""Join eligible candidates to their first subsequent VFS read and score paired sets."""
from bisect import bisect_right
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import struct
import sys


def summarize(directory, output):
    summary = {}
    with gzip.open(output / "candidates.jsonl.gz", "wt") as out:
        for seed in range(12345, 12357):
            reads = defaultdict(list)
            for ns, query, first, last in struct.iter_unpack("<QIII", (directory / f"{seed}.bin").read_bytes()):
                for page in range(first, last + 1):
                    reads[page].append((ns, query))
            counts = defaultdict(Counter)
            final_read_ns = ns
            for epoch, line in enumerate((directory / f"{seed}.jsonl").read_text().splitlines()):
                event = json.loads(line)
                start = event["ns"]
                q = event["query"]
                rows = []
                for page, last_read, pte in event["candidates"]:
                    stream = reads[page]
                    idx = bisect_right(stream, (start, float("inf")))
                    nxt, nq = stream[idx] if idx < len(stream) else (None, None)
                    rows.append(dict(page=page, last_read_ns=last_read, mincore_resident=True,
                                     pte=pte, next_ns=nxt, next_query=nq))
                k = min(event["budget"], len(rows))
                out.write(json.dumps(dict(seed=seed, epoch=epoch, ns=start, query=q, k=k,
                                          candidates=rows), separators=(",", ":")) + "\n")
                for horizon, complete in (("100ms", final_read_ns - start >= 100_000_000), ("64_queries", q + 63 <= 60000)):
                    if not complete:
                        continue
                    def early(row):
                        if row["next_ns"] is None:
                            return False
                        if horizon == "100ms":
                            return row["next_ns"] - start <= 100_000_000
                        return row["next_query"] < q + 64
                    c = counts[horizon]
                    c["epochs"] += 1
                    c["selections"] += k
                    for label, order in (("address", rows), ("lru", sorted(rows, key=lambda r: r["last_read_ns"])),
                                         ("oracle", sorted(rows, key=early))):
                        c[label] += sum(map(early, order[:k]))
            summary[seed] = counts
    (output / "counts.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    summarize(Path(sys.argv[1]), Path(sys.argv[2]))
