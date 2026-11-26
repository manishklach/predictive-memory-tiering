"""Verify PMT region observations against a separate workload with reversing hot/cold halves."""
import ctypes
import json
import mmap
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon"))
from live_sample import sampling

HALF = 4 << 20


def child():
    arena = mmap.mmap(-1, 2 * HALF, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
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
        start = phase[0] * HALF
        for pos in range(start, start + HALF, 4096):
            arena[pos]


def run():
    with subprocess.Popen([sys.executable, __file__, "--child"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as target:
        try:
            base = int(target.stdout.readline())
            ranges = [(base, base + HALF), (base + HALF, base + 2 * HALF)]
            phases = []
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


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        print(json.dumps(run(), indent=2))
