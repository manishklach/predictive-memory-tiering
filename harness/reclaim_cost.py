"""Fit expected tagged sleep per successful 10 ms reclaim from historical advice."""
from collections import defaultdict
import gzip
import json

from feature_ablation import Ablation
from pooled_predictor_replay import load_captures


def fit_tables(root, directory):
    grouped, _ = load_captures(root)
    idle = Ablation((1,))
    for groups in grouped.values():
        for seed, epochs in groups.items():
            if seed not in range(12345, 12357):
                raise ValueError("unexpected capture seed")
            for epoch in epochs:
                idle.train(epoch)
    prior = (idle.positives[()] + 16) / (idle.samples[()] + 32)
    idle_scores = {"fallback": prior}
    for key, count in idle.samples.items():
        if key:
            idle_scores[str(key[0])] = (idle.positives[key] + 32 * prior) / (count + 32)

    buckets = defaultdict(lambda: [0, 0, 0])
    for campaign in ("swap-fault-attribution", "filtered-fault-attribution"):
        with gzip.open(root / campaign / "trial.json.gz", "rt") as stream:
            trial = json.load(stream)
        for run in trial["runs"]:
            if not run["swap_faults"]:
                continue
            if run["query_seed"] not in range(12345, 12357):
                raise ValueError("unexpected attribution seed")
            for page, obs in run["residency_followup"].items():
                if obs["before"]["pte"] != "present" or not obs["before"]["mincore_resident"] or "10" not in obs:
                    continue
                ms = obs["last_read_age_ms"]
                age = -1 if ms is None else int(ms // 100).bit_length()
                stats = buckets[age]
                stats[0] += 1
                stats[1] += int(not obs["10"]["mincore_resident"])
                stats[2] += run["swap_faults"][page]["slept_offcpu_ns"]
    n, successes, sleep = map(sum, zip(*buckets.values()))
    costs = {"fallback": sleep / successes}
    for age, (_, freed, ns) in buckets.items():
        costs[str(age)] = (ns + 32 * sleep / n) / (freed + 32 * successes / n)
    for name, scores in (("idle", idle_scores), ("cost", costs)):
        (directory / f"{name}-frozen.json").write_text(json.dumps(scores))
