"""Build compact idle-only score tables outside the measured cgroup."""
import json
from feature_ablation import Ablation
from pooled_predictor_replay import load_captures


def prepare_idle(root, output):
    grouped, _ = load_captures(root)
    for seed in sorted(grouped["address"]):
        model = Ablation((1,))
        for groups in grouped.values():
            for other, epochs in groups.items():
                if other != seed:
                    for epoch in epochs:
                        model.train(epoch)
        base = (model.positives[()] + 16) / (model.samples[()] + 32)
        scores = {"fallback": base}
        for key, n in model.samples.items():
            if key:
                scores[str(key[0])] = (model.positives[key] + 32 * base) / (n + 32)
        (output / f"idle-{seed}.json").write_text(json.dumps(scores))
