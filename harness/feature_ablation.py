"""Factorial ablation of the pooled predictor under cross-capture seed holdout."""
from collections import Counter
from itertools import combinations
import json
from pathlib import Path

from page_reuse_predictor import Predictor, features, reused
from pooled_predictor_replay import load_captures


class Ablation(Predictor):
    def __init__(self, indices):
        super().__init__()
        self.indices = indices

    def keys(self, epoch, row):
        values = features(epoch, row)
        active = tuple(values[i] for i in self.indices)
        return [active[:n] for n in range(len(active) + 1)]


def evaluate(root):
    grouped, hashes = load_captures(root)
    prior = json.loads((root / "selector-trajectories/pooled-predictor.json").read_text())
    if hashes != prior["capture_sha256"]:
        raise ValueError("capture hashes differ from pooled replay")
    names = ("pte", "idle", "range")
    variants = {"+".join(names[i] for i in indices) or "none": indices
                for size in range(4) for indices in combinations(range(3), size)}
    folds = {name: {} for name in grouped}
    for seed in sorted(grouped["address"]):
        models = {name: Ablation(indices) for name, indices in variants.items()}
        for groups in grouped.values():
            for other, epochs in groups.items():
                if other != seed:
                    for epoch in epochs:
                        for model in models.values():
                            model.train(epoch)
        for name, groups in grouped.items():
            c = Counter()
            for epoch in groups[seed]:
                rows, k = epoch["candidates"], epoch["k"]
                lru = sorted(rows, key=lambda r: (r["last_read_ns"], r["page"]))
                c.update(epochs=1, selections=k, lru=sum(reused(epoch, r) for r in lru[:k]))
                for label, model in models.items():
                    ordered = sorted(rows, key=lambda r: (model.score(epoch, r), r["page"]))
                    c[label] += sum(reused(epoch, r) for r in ordered[:k])
            reference = prior["seeds"][name][str(seed)]
            if any(c[k] != reference[k] for k in ("epochs", "selections", "lru")) or c["pte+idle+range"] != reference["pooled"]:
                raise ValueError(f"pooled reference mismatch for {name}/{seed}")
            folds[name][seed] = c
    return dict(capture_sha256=hashes, seeds=folds,
                totals={name: sum(counts.values(), Counter()) for name, counts in folds.items()})


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "results/sydney-nvme-pilot"
    output = root / "selector-trajectories/feature-ablation.json"
    output.write_text(json.dumps(evaluate(root), indent=2) + "\n")
