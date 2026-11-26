"""Leave-one-seed-out evaluation on the Sydney candidate-headroom capture."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path


def features(epoch, row):
    last = row["last_read_ns"]
    # Never-read pages have a distinct bucket. Other buckets double from 100 ms.
    age = ((epoch["ns"] - last) // 100_000_000).bit_length() if last else -1
    return row["pte"], age, row["page"] // 128


def reused(epoch, row):
    return row["next_ns"] is not None and row["next_ns"] - epoch["ns"] <= 100_000_000


class Predictor:
    def __init__(self):
        self.samples = Counter()
        self.positives = Counter()

    def keys(self, epoch, row):
        pte, age, region = features(epoch, row)
        return [(), (pte,), (pte, age), (pte, age, region)]

    def train(self, epoch):
        for row in epoch["candidates"]:
            outcome = reused(epoch, row)
            for key in self.keys(epoch, row):
                self.samples[key] += 1
                self.positives[key] += outcome

    def score(self, epoch, row):
        risk = 0.5
        for key in self.keys(epoch, row):
            risk = (self.positives[key] + 32 * risk) / (self.samples[key] + 32)
        return risk


def prepare_folds(source, directory):
    with gzip.open(source, "rt") as stream:
        epochs = [json.loads(line) for line in stream]
    seeds = set(range(12345, 12357))
    if {e["seed"] for e in epochs} != seeds:
        raise ValueError("unexpected training seeds")
    for seed in seeds:
        model = Predictor()
        for epoch in epochs:
            if epoch["seed"] != seed:
                model.train(epoch)
        rows = [[key, n, model.positives[key]] for key, n in model.samples.items()]
        (directory / f"model-{seed}.json").write_text(json.dumps(rows))


def load_fold(directory, seed):
    model = Predictor()
    for key, n, positives in json.loads((directory / f"model-{seed}.json").read_text()):
        model.samples[tuple(key)] = n
        model.positives[tuple(key)] = positives
    return model


def evaluate(directory):
    source = directory / "candidates.jsonl.gz"
    with gzip.open(source, "rt") as stream:
        epochs = [json.loads(line) for line in stream]
    reference = json.loads((directory / "counts.json").read_text())
    totals = Counter()
    folds = {}
    for seed in sorted({e["seed"] for e in epochs}):
        model = Predictor()
        for e in epochs:
            if e["seed"] != seed:
                model.train(e)
        counts = Counter()
        for e in epochs:
            if e["seed"] != seed:
                continue
            rows = e["candidates"]
            orders = dict(address=sorted(rows, key=lambda r: r["page"]),
                          lru=sorted(rows, key=lambda r: (r["last_read_ns"], r["page"])),
                          predictor=sorted(rows, key=lambda r: (model.score(e, r), r["page"])),
                          oracle=sorted(rows, key=lambda r: reused(e, r)))
            counts["epochs"] += 1
            counts["selections"] += e["k"]
            for policy, order in orders.items():
                counts[policy] += sum(reused(e, r) for r in order[:e["k"]])
        # This particular capture has complete 100 ms observation for every epoch.
        if any(counts[k] != v for k, v in reference[str(seed)]["100ms"].items()):
            raise ValueError(f"incomplete window or baseline mismatch for seed {seed}")
        folds[seed] = counts
        totals.update(counts)
    return dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                validation="leave-one-seed-out", folds=folds, totals=totals)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(evaluate(args.directory), indent=2) + "\n")
