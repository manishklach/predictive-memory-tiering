"""Fresh Redis evaluation of age and one-second read count at constant reclaim."""
from itertools import permutations
import json
import os
from pathlib import Path
import random
import sys
import tempfile

from run import arm
from frequency_model import fit


def campaign(server, module, output):
    os.environ.update(PMT_FREQUENCY="1", PMT_CONSTANT_TARGET="1", PMT_BANDIT_TRIAL="1")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        def calibration(seed):
            row = arm(server, module, directory, "calibration", seed, 0, "no-flip")
            if row["swap_peak_bytes"]:
                raise ValueError("uncapped calibration swapped")
            return row["peak_bytes"] // 8192 * 4096
        os.environ["PMT_HOT_PAGES"] = "64"
        train_cap = calibration(42345)
        train = [arm(server, module, directory, "training", seed, train_cap, "no-flip")
                 for seed in range(42345, 42357)]
        (output.parent / "training.json").write_text(json.dumps(train))
        model = json.dumps(fit(train), sort_keys=True)
        (directory / "frequency-model.json").write_text(model)
        (output.parent / "frequency-model.json").write_text(model)
        os.environ["PMT_HOT_PAGES"] = "256"
        cap = calibration(52345)
        orders = list(permutations(("noswap", "age", "frequency"))) * 2
        random.Random(52701).shuffle(orders)
        runs = []
        for seed, order in enumerate(orders, 52345):
            rows = [arm(server, module, directory, mode, seed, cap, "no-flip") for mode in order]
            if len({(r["checksum"], r["first_half_sha256"], r["second_half_sha256"]) for r in rows}) != 1:
                raise ValueError("request stream mismatch")
            runs.extend(rows)
        output.write_text(json.dumps(dict(train_cap=train_cap, memory_max_bytes=cap, runs=runs)))


if __name__ == "__main__":
    campaign(*map(Path, sys.argv[1:]))
