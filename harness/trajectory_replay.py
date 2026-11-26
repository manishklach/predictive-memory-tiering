"""Replay two frozen selectors on both live trajectories with identical outcome windows."""
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import sys
import tempfile

from page_reuse_predictor import load_fold, prepare_folds, reused


def compare(output, training):
    results = {}
    with tempfile.TemporaryDirectory() as d:
        models = Path(d)
        prepare_folds(training, models)
        for mode in ("lru", "predictor"):
            folds = defaultdict(Counter)
            totals = Counter()
            with gzip.open(output / mode / "candidates.jsonl.gz", "rt") as stream:
                for line in stream:
                    event = json.loads(line)
                    rows = event["candidates"]
                    seed = event["seed"]
                    model = load_fold(models, seed)
                    k = event["k"]
                    picks = {
                        "lru": sorted(rows, key=lambda r: (r["last_read_ns"], r["page"]))[:k],
                        "predictor": sorted(rows, key=lambda r: (model.score(event, r), r["page"]))[:k],
                    }
                    if [r["page"] for r in picks[mode]] != event["selected"]:
                        raise ValueError(f"live/replay selection mismatch: {mode}, {seed}")
                    c = Counter(verified_epochs=1, verified_pages=k)
                    if event["complete_100ms"]:
                        c.update(epochs=1, selections=k, candidates=len(rows))
                        c["overlap"] = len({r["page"] for r in picks["lru"]} &
                                           {r["page"] for r in picks["predictor"]})
                        for policy, pages in picks.items():
                            c[policy] = sum(reused(event, r) for r in pages)
                    totals.update(c)
                    folds[seed].update(c)
            results[mode] = dict(totals=totals, seeds=folds)
    (output / "comparison.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    compare(Path(sys.argv[1]), Path(sys.argv[2]))
