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


def arm(server, module, directory, mode, seed, cap):
    group = Path(tempfile.mkdtemp(prefix="pmt-redis-", dir="/sys/fs/cgroup"))
    sock = directory / "server.sock"
    result = directory / "reply.json"
    result.unlink(missing_ok=True)
    env = dict(os.environ, PYTHONPATH=str(HERE), PMT_MODEL_DIR=str(directory),
               PMT_REDIS_MODULE=str(module), PMT_REDIS_RESULT=str(result),
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


def main(server, module, output):
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        fit_tables(HERE.parents[1] / "results/sydney-nvme-pilot", directory)
        reference = arm(server, module, directory, "calibration", 22345, 0)
        if reference["swap_peak_bytes"]:
            raise ValueError("uncapped calibration swapped")
        cap = reference["peak_bytes"] // 8192 * 4096
        orders = list(permutations(("cost", "noswap", "idle"))) * 2
        random.Random(1207).shuffle(orders)
        runs = []
        for i, order in enumerate(orders):
            seed = 22345 + i
            rows = [arm(server, module, directory, mode, seed, cap) for mode in order]
            if len({r["checksum"] for r in rows}) != 1:
                raise ValueError("checksum mismatch")
            runs.extend(rows)
        output.write_text(json.dumps(dict(version=subprocess.check_output([server,"--version"],
            text=True).strip(), calibration=reference, memory_max_bytes=cap, requests=60000, runs=runs)))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
