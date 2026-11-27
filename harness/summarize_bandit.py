"""Summarize delayed-bandit trials and check feedback against final kernel counters."""
import gzip
import json
from pathlib import Path
from statistics import median
import sys

from summarize_cost import summarize


def analyze(data):
    report = summarize(data, first_seed=32345, policies=("noswap", "cost", "bandit"))
    for run in data["runs"]:
        phases = run["phases"]
        traced = run["swap_faults"]
        total = sum(p["slept_offcpu_ns"] for p in traced.values())
        if phases["first"]["stall_ns"] + phases["second"]["stall_ns"] != total:
            raise ValueError("phase stall mismatch")
        for batch in phases["decisions"]:
            if "feedback_ns" in batch:
                if batch["feedback_ns"] < batch["ready_ns"]:
                    raise ValueError("premature feedback")
                if sum(traced[str(p)]["early_offcpu_ns"] for p in batch["pages"]) != batch["sleep_ns"]:
                    raise ValueError("feedback differs from final bounded sleep")
        if run["mode"] == "bounded-bandit-faulttrace":
            choices = [p for batch in phases["decisions"] for p in batch["pages"]]
            if len(set(choices)) != len(choices) or {str(p) for p in choices} != set(traced):
                raise ValueError("batch choices do not match advice")
    for mode, group in report["summary"].items():
        runs = [r for r in data["runs"] if r["mode"] == mode]
        for phase in ("first", "second"):
            items = [r["phases"][phase] for r in runs]
            group[phase] = dict(slept_ms=sum(p["stall_ns"] for p in items)/1e6,
                                p99_us=median(p["p99_us"] for p in items),
                                ops_per_s=median(p["ops_per_s"] for p in items))
    return report


if __name__ == "__main__":
    src, dst = map(Path, sys.argv[1:])
    with gzip.open(src, "rt") as stream:
        result = analyze(json.load(stream))
    dst.write_text(json.dumps(result, indent=2) + "\n")
