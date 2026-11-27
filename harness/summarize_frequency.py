"""Verify the frozen model and summarize held-out Redis frequency results."""
from collections import Counter
import gzip
import json
from math import comb
from pathlib import Path
from statistics import median
import sys

from frequency_model import fit
from summarize_cost import summarize


def report(directory):
    def read(name):
        with gzip.open(directory / name, "rt") as stream:
            return json.load(stream)
    training = read("training.json.gz")
    assert [r["query_seed"] for r in training] == list(range(42345, 42357))
    model = json.loads((directory / "frequency-model.json").read_text())
    assert fit(training) == model
    data = read("redis.json.gz")
    modes = ("noswap", "age", "frequency")
    result = summarize(data, first_seed=52345, policies=modes)
    by_mode = {r: result["seeds"][f"bounded-{r}-faulttrace"] for r in modes}
    for rows in by_mode.values():
        rows.sort(key=lambda r: r["seed"])
    by_seed = list(zip(*(by_mode[r] for r in modes)))
    matched = [rows for rows in by_seed if all(r["confirmed_bytes"] == 98304 for r in rows)]
    result["matched_seeds"] = [rows[0]["seed"] for rows in matched]
    result["matched"] = {}
    for i, mode in enumerate(modes):
        rows = [triplet[i] for triplet in matched]
        if rows:
            result["matched"][mode] = dict(stall_ms=sum(r["slept_ns"] for r in rows)/1e6,
                                          p99_us=median(r["p99_us"] for r in rows),
                                          ops_per_s=median(r["queries_per_s"] for r in rows))
    result["comparisons"] = {}
    for i, control in enumerate(modes[:2]):
        diffs = [rows[2]["slept_ns"] - rows[i]["slept_ns"] for rows in matched]
        wins, n = sum(d < 0 for d in diffs), sum(d != 0 for d in diffs)
        result["comparisons"][control] = dict(lower=wins, nonties=n, p=sum(comb(n,k) for k in range(wins,n+1))/2**n)
    order_counts = Counter(tuple(r["mode"] for r in data["runs"][i:i+3]) for i in range(0,36,3))
    assert len(order_counts) == 6 and set(order_counts.values()) == {2}
    return result


if __name__ == "__main__":
    print(json.dumps(report(Path(sys.argv[1])), indent=2))
