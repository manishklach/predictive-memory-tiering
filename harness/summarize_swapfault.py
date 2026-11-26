"""Summarize per-seed swap-fault attribution and observed page residency."""
from collections import defaultdict
import json
from pathlib import Path
from statistics import median
import sys


def summarize(trial):
    if trial["memory_max_bytes"] != 19660800 or trial["requests"] != 60000:
        raise ValueError("unexpected workload")
    groups = defaultdict(list)
    for run in trial["runs"]:
        ev = run["residency_followup"]
        traced = run["swap_faults"]
        if traced is not None and set(traced) != set(ev):
            raise ValueError("attribution keys do not match advice")
        row = dict(seed=run["query_seed"], advised=len(ev), p99_us=run["p99_us"],
                   queries_per_s=run["queries_per_s"])
        for when in ("immediate", "10", "100"):
            key = f"nonresident_bytes_{when}"
            row[key] = sum(4096 for p in ev.values()
                           if p["before"]["pte"] == "present" and p["before"]["mincore_resident"]
                           and when in p and not p[when]["mincore_resident"])
            row[f"observed_{when}"] = sum(when in p for p in ev.values())
        if traced is not None:
            for field in ("calls", "slept_offcpu_ns", "handler_ns", "swapin_delay_ns"):
                row[field] = sum(p[field] for p in traced.values())
            row["faulted_pages"] = sum(p["calls"] > 0 for p in traced.values())
        groups[run["mode"]].append(row)
    if len(groups) != 4:
        raise ValueError("missing arms")
    summary = {}
    for mode, rows in groups.items():
        if sorted(r["seed"] for r in rows) != list(range(12345, 12357)):
            raise ValueError("missing seeds")
        summary[mode] = {k: (median(r[k] for r in rows) if k in ("p99_us", "queries_per_s")
                             else sum(r[k] for r in rows)) for k in rows[0] if k != "seed"}
    return dict(summary=summary, per_seed=groups)


if __name__ == "__main__":
    src, dst = map(Path, sys.argv[1:])
    dst.write_text(json.dumps(summarize(json.loads(src.read_text())), indent=2) + "\n")
