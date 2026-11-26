"""Run the local arena in fresh delegated cgroup v2 leaves at half its control peak."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
PAGES = 4096
ROUNDS = 3


def run(parent, mode, limit, core):
    group = Path(tempfile.mkdtemp(prefix="pmt-", dir=parent))
    try:
        if limit:
            (group / "memory.max").write_text(str(limit))
        command = ["./pmt-local", mode, str(PAGES), str(PAGES // 2 if limit else PAGES),
                   str(ROUNDS), "10", "1", "0"]
        if mode != "stock":
            command.append(str(core))
        env = {**os.environ, "PMT_WORKLOAD_CGROUP": str(group)}
        swap_peak = 0
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            while process.poll() is None:
                swap_peak = max(swap_peak, int((group / "memory.swap.current").read_text()))
                time.sleep(.01)
            out, err = process.communicate()
        return {"mode": mode, "exit_code": process.returncode, "stderr": err,
                "limit": (group / "memory.max").read_text().strip(),
                "peak_bytes": int((group / "memory.peak").read_text()),
                "sampled_peak_swap_bytes": swap_peak,
                "events": dict(line.split() for line in (group / "memory.events").read_text().splitlines()),
                "arena": json.loads(out) if out else None}
    finally:
        group.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--parent", type=Path, default=Path("/sys/fs/cgroup"))
    args = parser.parse_args()
    core = min(os.sched_getaffinity(0))
    calibration = run(args.parent, "stock", None, core)
    if calibration["exit_code"] or calibration["sampled_peak_swap_bytes"]:
        raise SystemExit("control failed or swapped")
    page_size = os.sysconf("SC_PAGE_SIZE")
    limit = calibration["peak_bytes"] // (2 * page_size) * page_size
    report = {"kernel": platform.release(), "scope": "local anonymous arena",
              "pages": PAGES, "rounds": ROUNDS, "controller_cpu": core,
              "swap": Path("/proc/swaps").read_text().splitlines(),
              "sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                         for name in ("pmtd", "pmt-local")},
              "calibration": calibration, "hard_limit_bytes": limit, "runs": []}
    modes = ["stock", "none", "stride", "markov"]
    for repeat in range(3):
        for mode in modes[repeat:] + modes[:repeat]:
            record = run(args.parent, mode, limit, core)
            report["runs"].append({"repeat": repeat + 1, **record})
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            if record["exit_code"]:
                raise SystemExit(f"{mode} failed; see {args.output}")


if __name__ == "__main__":
    main()
