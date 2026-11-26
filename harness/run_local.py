"""Compare cooperative policies and record local correctness evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=128)
    parser.add_argument("--budget-pages", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--idle-ms", type=int, default=8)
    parser.add_argument("--dwell-ms", type=int, default=2)
    parser.add_argument("--delay-us", type=int, default=1000)
    parser.add_argument("--controller-cpu", type=int)
    args = parser.parse_args()
    if not 2 <= args.pages <= 8192 or not 1 <= args.budget_pages <= args.pages:
        parser.error("expected 2–8192 pages and a budget within the arena")
    if not 1 <= args.rounds <= 1_000_000 // args.pages:
        parser.error("round count must produce between one pass and one million accesses")
    if not (1 <= args.idle_ms <= 3_600_000 and 0 <= args.dwell_ms <= 3_600_000
            and 0 <= args.delay_us <= 1_000_000):
        parser.error("invalid idle, dwell, or inter-access delay")

    binaries = [ROOT / "pmtd", ROOT / "pmt-local"]
    if not all(binary.is_file() for binary in binaries):
        parser.error("run make from the repository root first")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip() or None
    cgroup = os.environ.get("PMT_WORKLOAD_CGROUP")
    memory_limits = None
    if cgroup:
        memory_limits = {
            name: (Path(cgroup) / name).read_text().strip()
            for name in ("memory.max", "memory.swap.max")
        }
    report = {
        "schema_version": 1,
        "mode": "cooperative_linux_correctness",
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "page_bytes": os.sysconf("SC_PAGE_SIZE"),
        "swap": Path("/proc/swaps").read_text().splitlines(),
        "workload_cgroup_limits": memory_limits,
        "git_head": commit,
        "binary_sha256": {binary.name: sha256(binary) for binary in binaries},
        "settings": {key: value for key, value in vars(args).items() if key != "out"},
        "runs": [],
    }
    timeout = 30 + args.pages * args.rounds * (args.delay_us / 1e6 + .002)
    for label, mode, budget in (
        ("stock_unconstrained", "stock", args.pages),
        ("stock", "stock", args.budget_pages),
        ("detector", "none", args.budget_pages),
        ("stride", "stride", args.budget_pages),
        ("markov", "markov", args.budget_pages),
    ):
        command = [
            str(binaries[1]), mode, str(args.pages), str(budget), str(args.rounds),
            str(args.idle_ms), str(args.dwell_ms), str(args.delay_us),
        ]
        if args.controller_cpu is not None and mode != "stock":
            command.append(str(args.controller_cpu))
        environment = os.environ.copy()
        if label == "stock_unconstrained":
            environment.pop("PMT_WORKLOAD_CGROUP", None)
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=timeout,
        )
        run = {"label": label, "exit_code": result.returncode}
        if result.stdout.strip():
            try:
                run.update(json.loads(result.stdout))
            except ValueError:
                run["invalid_output"] = result.stdout
        if result.stderr:
            run["stderr"] = result.stderr
        report["runs"].append(run)
        if result.returncode or "accesses" not in run:
            break
    report["complete"] = len(report["runs"]) == 5 and all(
        run["exit_code"] == 0 and "accesses" in run for run in report["runs"]
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=args.out.parent, delete=False,
                                     encoding="utf-8") as staging:
        json.dump(report, staging, indent=2)
        staging.write("\n")
    os.replace(staging.name, args.out)
    print(args.out)
    if not report["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
