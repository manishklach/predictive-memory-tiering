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


def arm(server, module, directory, mode, seed, cap, pattern="flip"):
    group = Path(tempfile.mkdtemp(prefix="pmt-redis-", dir="/sys/fs/cgroup"))
    sock = directory / "server.sock"
    result = directory / "reply.json"
    result.unlink(missing_ok=True)
    env = dict(os.environ, PYTHONPATH=str(HERE), PMT_MODEL_DIR=str(directory),
               PMT_HOT_PATTERN=pattern, PMT_REDIS_MODULE=str(module), PMT_REDIS_RESULT=str(result),
               PMT_REDIS_MODE=mode, PMT_REDIS_SEED=str(seed))
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


def main(server, module, output, flip_control=False, flip_after=30000):
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
            patterns = ("flip", "no-flip") if flip_control else ("flip",)
            if i % 2:
                patterns = patterns[::-1]
            for pattern in patterns:
                rows = [arm(server, module, directory, mode, seed, cap, pattern) for mode in order]
                if len({r["checksum"] for r in rows}) != 1:
                    raise ValueError("checksum mismatch")
                for row in rows:
                    row["pattern"] = pattern
                runs.extend(rows)
        output.write_text(json.dumps(dict(version=subprocess.check_output([server,"--version"],
            text=True).strip(), flip_after=flip_after, constant_target=flip_control, calibration=reference, memory_max_bytes=cap, requests=60000, runs=runs)))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    for key in ("server", "module", "output"):
        parser.add_argument(key, type=Path)
    parser.add_argument("--flip-control", action="store_true")
    parser.add_argument("--flip-after", type=int, default=30000)
    main(**vars(parser.parse_args()))
