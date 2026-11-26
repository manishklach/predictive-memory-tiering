"""Fit the existing page predictor on three pilot captures with grouped seed holdout."""
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path

from page_reuse_predictor import Predictor, reused


def load_captures(root):
    cross = root / "selector-trajectories"
    paths = {"address": root / "candidate-headroom", "lru": cross / "lru",
             "predictor": cross / "predictor"}
    seeds = list(range(12345, 12357))
    grouped = {}
    hashes = {}
    for name, directory in paths.items():
        capture = directory / "candidates.jsonl.gz"
        hashes[name] = hashlib.sha256(capture.read_bytes()).hexdigest()
        groups = defaultdict(list)
        with gzip.open(capture, "rt") as stream:
            for line in stream:
                event = json.loads(line)
                # All epochs in the original address capture have complete windows.
                if name == "address" or event["complete_100ms"]:
                    groups[event["seed"]].append(event)
        if sorted(groups) != seeds:
            raise ValueError(f"incomplete seed coverage in {name}")
        grouped[name] = groups

    return grouped, hashes


def evaluate(root):
    grouped, hashes = load_captures(root)
    cross = root / 'selector-trajectories'
    previous = json.loads((cross / "comparison.json").read_text())
    address_reference = json.loads((root / "candidate-headroom" / "predictor.json").read_text())
    seeds = sorted(grouped['address'])
    results = {name: {} for name in grouped}
    for held_out in seeds:
        pooled = Predictor()
        original = Predictor()
        for name, groups in grouped.items():
            for seed, epochs in groups.items():
                if seed == held_out:
                    continue
                for epoch in epochs:
                    pooled.train(epoch)
                    if name == "address":
                        original.train(epoch)
        for name, groups in grouped.items():
            counts = Counter()
            for epoch in groups[held_out]:
                rows = epoch["candidates"]
                k = epoch["k"]
                orders = {"lru": sorted(rows, key=lambda r: (r["last_read_ns"], r["page"]))}
                for label, model in (("original", original), ("pooled", pooled)):
                    orders[label] = sorted(rows, key=lambda r: (model.score(epoch, r), r["page"]))
                counts.update(epochs=1, selections=k)
                for label, order in orders.items():
                    counts[label] += sum(reused(epoch, row) for row in order[:k])
            ref = (address_reference["folds"][str(held_out)] if name == "address"
                   else previous[name]["seeds"][str(held_out)])
            for key, old in (("epochs", "epochs"), ("selections", "selections"),
                             ("lru", "lru"), ("original", "predictor")):
                if counts[key] != ref[old]:
                    raise ValueError(f"reference mismatch for {name}/{held_out}/{key}")
            results[name][held_out] = counts
    return dict(capture_sha256=hashes, seeds=results,
                totals={name: sum(folds.values(), Counter()) for name, folds in results.items()})


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "results/sydney-nvme-pilot"
    output = root / "selector-trajectories/pooled-predictor.json"
    output.write_text(json.dumps(evaluate(root), indent=2) + "\n")
