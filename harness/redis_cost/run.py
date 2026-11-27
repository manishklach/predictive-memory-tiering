"""Start an isolated Redis server per arm. Run as root on the disposable pilot."""
from itertools import permutations
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from reclaim_cost import fit_tables


def arm(server, module, directory, mode, seed, cap, pattern="flip", boundary=None, anchor=False):
    group = Path(tempfile.mkdtemp(prefix="pmt-redis-", dir="/sys/fs/cgroup"))
    sock = directory / "server.sock"
    result = directory / "reply.json"
    result.unlink(missing_ok=True)
    env = dict(os.environ, PYTHONPATH=str(HERE), PMT_MODEL_DIR=str(directory),
               PMT_HOT_PATTERN=pattern, PMT_REDIS_MODULE=str(module), PMT_REDIS_RESULT=str(result),
               PMT_REDIS_MODE=mode, PMT_REDIS_SEED=str(seed))
    if boundary is not None:
        env["PMT_FLIP_AFTER"] = str(boundary)
    env["PMT_FEEDBACK_ANCHOR"] = str(int(anchor))
    proc = None
    try:
        if cap:
            (group / "memory.max").write_text(str(cap))
        def enter():
            (group / "cgroup.procs").write_text(str(os.getpid()))
        with (directory / "server.log").open("w") as log:
            proc = subprocess.Popen([server, "--port", "0", "--unixsocket", str(sock),
                "--unixsocketperm", "600", "--save", "", "--appendonly", "no",
                "--dir", str(directory), "--loadmodule", module], preexec_fn=enter,
                env=env, stdout=log, stderr=log)
            for _ in range(500):
                if sock.exists() or proc.poll() is not None:
                    break
                time.sleep(.02)
            reply = subprocess.run(["redis-cli", "-s", sock, "pmtbench.run"],
                                   capture_output=True, text=True, timeout=120)
            if reply.stdout.strip() != "OK" or not result.exists():
                raise RuntimeError(f"{reply.stdout} {reply.stderr}\n{(directory/'server.log').read_text()[-2500:]}")
            row = json.loads(result.read_text())
            row["peak_bytes"] = int((group / "memory.peak").read_text())
            row["swap_peak_bytes"] = int((group / "memory.swap.peak").read_text())
            return row
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        sock.unlink(missing_ok=True)
        group.rmdir()


def balanced_orders():
    """Randomized complete blocks with two appearances per condition per position."""
    rng = random.Random(20261127)
    orders = []
    for _ in range(2):
        cells = [(pattern, mode) for pattern in ("flip", "no-flip")
                 for mode in ("bandit", "noswap", "cost")]
        rng.shuffle(cells)
        orders.extend(cells[i:] + cells[:i] for i in range(6))
    rng.shuffle(orders)
    return orders


def main(server, module, output, flip_control=False, flip_after=30000, feedback_flip=False,
         recorded_flip=False):
    boundaries = {}
    if recorded_flip:
        if not flip_control or feedback_flip or flip_after != 30000:
            raise ValueError("recorded flip requires paired controls without a live or fixed override")
        source = HERE.parents[1] / "results/sydney-nvme-pilot/redis-feedback-flip/summary.json"
        boundaries = {seed: event["completed"] for seed, event in json.loads(source.read_text())["triggers"].items()}
        schedule = balanced_orders()
    if feedback_flip and not flip_control:
        raise ValueError("feedback flip requires paired controls")
    if not 0 < flip_after < 60000 or flip_after % 2:
        raise ValueError('flip-after must be an even operation count between 0 and 60000')
    if flip_after != 30000 and not flip_control:
        raise ValueError('custom boundary requires --flip-control')
    os.environ['PMT_FLIP_AFTER'] = str(flip_after)
    if flip_control:
        os.environ.update(PMT_BANDIT_TRIAL="1", PMT_CONSTANT_TARGET="1")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        fit_tables(HERE.parents[1] / "results/sydney-nvme-pilot", directory)
        bandit = os.environ.get("PMT_BANDIT_TRIAL") == "1"
        first_seed = 32345 if bandit else 22345
        reference = arm(server, module, directory, "calibration", first_seed, 0)
        if reference["swap_peak_bytes"]:
            raise ValueError("uncapped calibration swapped")
        cap = reference["peak_bytes"] // 8192 * 4096
        orders = list(permutations(("cost", "noswap", "bandit" if bandit else "idle"))) * 2
        random.Random(1207).shuffle(orders)
        runs = []
        for i, order in enumerate(orders):
            seed = first_seed + i
            if recorded_flip:
                for position, (pattern, mode) in enumerate(schedule[i], 1):
                    row = arm(server, module, directory, mode, seed, cap, pattern, boundaries[str(seed)])
                    row.update(pattern=pattern, position=position)
                    runs.append(row)
                continue
            patterns = ("flip", "no-flip") if flip_control else ("flip",)
            if i % 2:
                patterns = patterns[::-1]
            if feedback_flip:
                anchor = arm(server, module, directory, "bandit", seed, cap, "flip", 60000, True)
                anchor["pattern"] = "flip"
                runs.append(anchor)
                for pattern in patterns:
                    for mode in order:
                        if (pattern, mode) == ("flip", "bandit"):
                            continue
                        row = arm(server, module, directory, mode, seed, cap, pattern, anchor["boundary"])
                        row["pattern"] = pattern
                        runs.append(row)
                continue
            for pattern in patterns:
                rows = [arm(server, module, directory, mode, seed, cap, pattern) for mode in order]
                if len({r["checksum"] for r in rows}) != 1:
                    raise ValueError("checksum mismatch")
                for row in rows:
                    row["pattern"] = pattern
                runs.extend(rows)
        output.write_text(json.dumps(dict(version=subprocess.check_output([server,"--version"],
            text=True).strip(), recorded_boundaries=boundaries, feedback_flip=feedback_flip, flip_after=None if feedback_flip or recorded_flip else flip_after, constant_target=flip_control, calibration=reference, memory_max_bytes=cap, requests=60000, runs=runs)))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    for key in ("server", "module", "output"):
        parser.add_argument(key, type=Path)
    parser.add_argument("--flip-control", action="store_true")
    parser.add_argument("--recorded-flip", action="store_true")
    parser.add_argument("--feedback-flip", action="store_true")
    parser.add_argument("--flip-after", type=int, default=30000)
    main(**vars(parser.parse_args()))
