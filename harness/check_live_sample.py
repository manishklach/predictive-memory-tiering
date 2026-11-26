"""Verify PMT region observations against a separate workload with reversing hot/cold halves."""
import ctypes
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon"))
from live_sample import ADMIN, sampling

HALF = 32 << 20
GAP = mmap.PAGESIZE


def child():
    arena = mmap.mmap(-1, 2 * HALF + GAP, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    arena.madvise(mmap.MADV_NOHUGEPAGE)
    for pos in range(0, len(arena), 4096):
        arena[pos] = 1
    base = ctypes.addressof(ctypes.c_char.from_buffer(arena))
    phase = [0]

    def requests():
        for cmd in sys.stdin:
            phase[0] = int(cmd)
            print("ready", flush=True)
        os._exit(0)

    print(base, flush=True)
    threading.Thread(target=requests, daemon=True).start()
    while True:
        start = phase[0] * (HALF + GAP)
        for pos in range(start, start + HALF, 4096):
            arena[pos]


def run(score=False):
    with subprocess.Popen([sys.executable, __file__, "--child"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as target:
        try:
            base = int(target.stdout.readline())
            ranges = [(base, base + HALF), (base + HALF + GAP, base + 2 * HALF + GAP)]
            phases = []
            if score:
                from region_detector import RegionDetector
                detector = RegionDetector()
                with sampling(target.pid, ranges, detector):
                    for hot in (0, 1, 0, 1):
                        target.stdin.write(f"{hot}\n")
                        target.stdin.flush()
                        assert target.stdout.readline().strip() == "ready"
                        begin = time.monotonic_ns()
                        time.sleep(1.8)
                        end = time.monotonic_ns()
                        phases.append((hot, begin, end))
                report = []
                for hot, begin, end in phases:
                    counts = dict(cold=0, hot=0, other=0, abstain=0)
                    early = dict.fromkeys(counts, 0)
                    for stamp, region in detector.decisions:
                        if not begin <= stamp < end:
                            continue
                        outcome = "abstain"
                        if region:
                            outcome = "other"
                            for i, (low, high) in enumerate(ranges):
                                if low <= region[0] < region[1] <= high:
                                    outcome = "hot" if i == hot else "cold"
                        bucket = early if stamp - begin < 400_000_000 else counts
                        bucket[outcome] += 1
                    report.append(dict(hot_half=hot, transition=early, settled=counts))
                for phase in report:
                    votes = phase["settled"]
                    if votes["cold"] < 5 or votes["cold"] < .9 * sum(votes.values()):
                        raise RuntimeError(f"cold-region verification failed: {report}")
                return dict(kernel=os.uname().release, phase_ms=1800, transition_ms=400,
                            half_bytes=HALF, pageout_requests=0, phases=report)
            for hot in (0, 1, 0, 1):
                target.stdin.write(f"{hot}\n")
                target.stdin.flush()
                assert target.stdout.readline().strip() == "ready"
                with sampling(target.pid, ranges) as samples:
                    time.sleep(1.5)
                means = []
                for low, high in ranges:
                    weights = [(min(end, high) - max(start, low), hits)
                               for start, end, hits, age in samples if end > low and start < high]
                    means.append(sum(n*h for n,h in weights) / sum(n for n,h in weights))
                if means[hot] < 5 or means[hot] <= 3 * means[1-hot]:
                    raise RuntimeError(f"region contrast failed: hot={hot}, means={means}")
                phases.append(dict(hot_half=hot, records=len(samples), mean_hits_per_half=means))
            return dict(kernel=os.uname().release, half_bytes=HALF, phases=phases)
        finally:
            target.terminate()


def exit_checks():
    sampler = Path(__file__).resolve().parents[1] / "daemon/live_sample.py"
    for stop_target in (True, False):
        with subprocess.Popen(["sleep", "20"]) as target:
            with subprocess.Popen([sys.executable, sampler, str(target.pid), "--seconds", "15"],
                                  stdout=subprocess.PIPE, text=True) as reader:
                try:
                    state = ADMIN / "0/state"
                    deadline = time.monotonic() + 5
                    while not (state.exists() and state.read_text().strip() == "on"):
                        if reader.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("sampler did not attach")
                        time.sleep(.02)
                    time.sleep(.25)
                    (target if stop_target else reader).terminate()
                    stdout, _ = reader.communicate(timeout=3)
                    if reader.returncode or (stop_target and json.loads(stdout)["pid"] != target.pid):
                        raise RuntimeError("sampler shutdown failed")
                    assert (ADMIN / "nr_kdamonds").read_text().strip() == "0"
                finally:
                    target.terminate()
                    if reader.poll() is None:
                        reader.terminate()
    return {"target_exit": "passed", "reader_sigterm": "passed"}


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        print(json.dumps({**run("--score" in sys.argv), "shutdown": exit_checks()}, indent=2))
