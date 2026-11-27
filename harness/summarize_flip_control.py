"""Verify the paired Redis streams and summarize both constant-allowance conditions."""
from array import array
import gzip
import hashlib
import json
import random
from pathlib import Path
from statistics import median
import sys

from summarize_bandit import analyze


def summarize_control(data):
    if not data["constant_target"] or len(data["runs"]) != 72:
        raise ValueError("expected 72 constant-target runs")
    boundary = data.get('flip_after', 30000)
    conditions = {}
    indexed = {}
    for pattern in ("flip", "no-flip"):
        runs = [r for r in data["runs"] if r["pattern"] == pattern]
        conditions[pattern] = analyze(dict(data, runs=runs))
        indexed[pattern] = {(r["mode"], r["query_seed"]): r for r in runs}
    triggers = {}
    for seed in range(32345, 32357):
        if data.get("recorded_boundaries"):
            boundary = data["recorded_boundaries"][str(seed)]
        if data.get("feedback_flip"):
            anchor = indexed["flip"]["bounded-bandit-faulttrace", seed]
            boundary = anchor["boundary"]
            event = anchor["trigger"]
            if event:
                batches = anchor["phases"]["decisions"]
                mature = {b["arm"] for b in batches if b.get("feedback_ns", event["time_ns"] + 1) <= event["time_ns"]}
                phases = anchor["phases"]
                if (mature != {"cost", "noswap"} or boundary != event["completed"] or
                    boundary % 128 or phases["mid_advised"] >= 64 or event["slots"] <= 0 or
                    event["slots"] != 24 - phases["mid_confirmed"] - event["outstanding"]):
                    raise ValueError("invalid feedback gate")
            elif boundary != data["requests"]:
                raise ValueError("missing trigger")
            triggers[seed] = event
        rng = random.Random(seed)
        stream = array("I", (rng.randrange(8192 if i % 2 else 64) for i in range(60000)))
        prefix = hashlib.sha256(stream[:boundary]).hexdigest()
        for pattern in ("no-flip", "flip"):
            if pattern == "flip":
                for i in range(boundary, len(stream), 2):
                    stream[i] += 4096
            suffix = hashlib.sha256(stream[boundary:]).hexdigest()
            checksum = sum(page % 251 for page in stream)
            for mode in conditions[pattern]["summary"]:
                r = indexed[pattern][mode, seed]
                if r.get("boundary", boundary) != boundary:
                    raise ValueError("unmatched boundary")
                if (r["first_half_sha256"], r["second_half_sha256"], r["checksum"]) != (prefix, suffix, checksum):
                    raise ValueError(f"draw or reply mismatch for {pattern}/{mode}/{seed}")
    paired = {}
    for mode in conditions["flip"]["summary"]:
        pairs = [(indexed["flip"][mode, seed], indexed["no-flip"][mode, seed])
                 for seed in range(32345, 32357)]
        def full(row):
            return sum(obs["before"]["pte"] == "present" and obs["before"]["mincore_resident"]
                       and "10" in obs and not obs["10"]["mincore_resident"]
                       for obs in row["residency_followup"].values()) == 24
        matched = [(a,b) for a,b in pairs if full(a) and full(b) 
                   and (not triggers or triggers[a["query_seed"]] is not None)]
        metrics = {}
        for a,b in pairs:
            if a["first_half_sha256"] != b["first_half_sha256"] or (a["second_half_sha256"] == b["second_half_sha256"]) != (a.get("boundary") == data["requests"]):
                raise ValueError("workload streams do not pair")
        for pattern, index in (("flip",0),("no-flip",1)):
            rows = [pair[index] for pair in pairs]
            rr = [pair[index] for pair in matched]
            metrics[pattern] = dict(midpoint_target_hits=sum(r["phases"]["mid_confirmed"]==24 for r in rows),
                after_midpoint_advice=sum(len(r["swap_faults"])-r["phases"]["mid_advised"] for r in rows),
                runs_with_after_midpoint_advice=sum(len(r["swap_faults"])>r["phases"]["mid_advised"] for r in rows),
                matched_total_stall_ms=sum(p["slept_offcpu_ns"] for r in rr for p in r["swap_faults"].values())/1e6 if rr else None,
                matched_p99_us=median(r["p99_us"] for r in rr) if rr else None,
                matched_ops_per_s=median(r["queries_per_s"] for r in rr) if rr else None)
        paired[mode] = dict(matched_seeds=[a["query_seed"] for a,b in matched], **metrics)
    shared = sorted(set.intersection(*(set(v["matched_seeds"]) for v in paired.values())))
    six_way = {}
    for pattern, by_key in indexed.items():
        six_way[pattern] = {}
        for mode in paired:
            rows = [by_key[mode, seed] for seed in shared]
            if rows:
                six_way[pattern][mode] = dict(
                    stall_ms=sum(p["slept_offcpu_ns"] for r in rows for p in r["swap_faults"].values()) / 1e6,
                    p99_us=median(r["p99_us"] for r in rows),
                    ops_per_s=median(r["queries_per_s"] for r in rows))
    return dict(triggers=triggers, flip_after=data.get("flip_after", 30000), memory_max_bytes=data["memory_max_bytes"], conditions=conditions, paired=paired,
                six_way_matched_seeds=shared, six_way_matched=six_way)


if __name__ == "__main__":
    src, dst = map(Path, sys.argv[1:])
    with gzip.open(src,"rt") as stream:
        result = summarize_control(json.load(stream))
    dst.write_text(json.dumps(result,indent=2)+"\n")
