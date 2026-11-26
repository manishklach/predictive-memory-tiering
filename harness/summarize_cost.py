"""Summarize the matched 24-page reclaim experiment."""
from collections import defaultdict
import gzip
import json
from pathlib import Path
from statistics import median
import sys


def summarize(data):
    groups = defaultdict(list)
    for run in data["runs"]:
        pages = run["residency_followup"]
        faults = run["swap_faults"]
        assert set(pages) == set(faults) and run["task_swap_calls"] > 0
        regained = sum(p["before"]["pte"] == "present" and p["before"]["mincore_resident"]
                       and "10" in p and not p["10"]["mincore_resident"] for p in pages.values())
        if regained > 24:
            raise ValueError("target overshoot")
        groups[run["mode"]].append(dict(seed=run["query_seed"], confirmed_bytes=4096*regained,
            advised=len(pages), slept_ns=sum(p["slept_offcpu_ns"] for p in faults.values()),
            present_before_slept_ns=sum(faults[k]["slept_offcpu_ns"] for k,p in pages.items()
                                        if p["before"]["pte"]=="present"),
            p99_us=run["p99_us"], queries_per_s=run["queries_per_s"]))
    expected = {"bounded-" + s + "-faulttrace" for s in ("noswap","idle","cost")}
    assert set(groups) == expected
    for rows in groups.values():
        assert sorted(r["seed"] for r in rows) == list(range(22345,22357))
    summary = {}
    for mode, rows in groups.items():
        summary[mode] = dict(target_met=sum(r["confirmed_bytes"]==98304 for r in rows),
            confirmed_bytes=sum(r["confirmed_bytes"] for r in rows),
            advised=sum(r["advised"] for r in rows), slept_ms=sum(r["slept_ns"] for r in rows)/1e6,
            present_before_slept_ms=sum(r["present_before_slept_ns"] for r in rows)/1e6,
            p99_us=median(r["p99_us"] for r in rows), queries_per_s=median(r["queries_per_s"] for r in rows))
    return dict(target_bytes=98304, summary=summary, seeds=groups)


if __name__ == "__main__":
    src, dst = map(Path, sys.argv[1:])
    with gzip.open(src,"rt") as stream:
        result=summarize(json.load(stream))
    dst.write_text(json.dumps(result,indent=2)+"\n")
