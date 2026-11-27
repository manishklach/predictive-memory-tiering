"""Timed server-side native GETRANGE calls on an anonymous Redis string value."""
from array import array
from contextlib import nullcontext
import ctypes
import json
import mmap
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "harness"), str(ROOT / "daemon")]
from bounded_eviction import BoundedEviction
from live_sample import sampling
from region_detector import RegionDetector
from residency_followup import ResidencyFollowup
from swap_trace import SwapTrace


class RedisValue:
    def __init__(self, address, size, libc):
        self.address, self.size, self.libc = address, size, libc

    def __len__(self):
        return self.size

    def madvise(self, advice, offset, size):
        if self.libc.madvise(ctypes.c_void_p(self.address + offset), size, advice):
            raise OSError(ctypes.get_errno(), "madvise")


def run(address, pages):
    mode = os.environ["PMT_REDIS_MODE"]
    seed = int(os.environ["PMT_REDIS_SEED"])
    directory = Path(os.environ["PMT_MODEL_DIR"])
    libc = ctypes.CDLL(None, use_errno=True)
    getrange = ctypes.PyDLL(os.environ["PMT_REDIS_MODULE"]).pmt_getrange
    getrange.argtypes = [ctypes.c_int]
    obj = RedisValue(address, pages * mmap.PAGESIZE, libc)
    selector = BoundedEviction(obj, address, libc, lru=True, skip_swapped=mode in ("cost", "noswap"))
    adaptive_trial = os.environ.get("PMT_BANDIT_TRIAL") == "1"
    if mode == "bandit":
        selector.skip_swapped = True
    if mode in ("cost", "idle", "bandit"):
        selector.idle_risk = json.loads((directory / ("cost-frozen.json" if mode == "bandit" else f"{mode}-frozen.json")).read_text())
    selector.target_pages = 24
    detector = RegionDetector()
    traced = mode != "calibration"
    monitor = sampling(os.getpid(), [(address, address + len(obj))], detector, buffer_kb=32) if traced else nullcontext()
    rng = random.Random(seed)
    draws = array("I", (rng.randrange(pages if i % 2 else 64) for i in range(60000)))
    schedule = None
    if adaptive_trial:
        for i in range(30000, len(draws), 2):
            draws[i] += pages // 2
        if traced:
            from batch_bandit import TrialSchedule
            schedule = TrialSchedule(selector, mode, seed, len(draws))
    times = array("Q")
    consumed = checksum = 0
    with monitor:
        if traced:
            selector.followup = ResidencyFollowup(selector)
            selector.swap_trace = SwapTrace()
        start = time.monotonic_ns()
        for page in draws:
            t = time.monotonic_ns()
            if traced:
                selector.followup.poll(t)
                if schedule:
                    schedule.step(len(times) + 1, t, detector, start)
                elif consumed < len(detector.decisions):
                    consumed = len(detector.decisions)
                    region = detector.decisions[consumed - 1][1]
                    if region:
                        selector.apply(region)
                selector.on_read(page * mmap.PAGESIZE, 256)
            value = getrange(page)
            if value != page % 251:
                raise ValueError("incorrect GETRANGE reply")
            times.append(time.monotonic_ns() - t)
            checksum += value
        elapsed = time.monotonic_ns() - start
        faults = selector.swap_trace.finish() if traced else None
        if traced:
            selector.followup.close()
    row = dict(phases=schedule.report(times, elapsed, faults) if schedule else None, query_seed=seed, checksum=checksum, mode=f"bounded-{mode}-faulttrace",
               p99_us=sorted(times)[59399]/1000, queries_per_s=len(times)*1e9/elapsed,
               task_swap_calls=selector.swap_trace.task_faults if traced else None,
               swap_faults=faults, residency_followup=selector.followup.pages if traced else None)
    Path(os.environ["PMT_REDIS_RESULT"]).write_text(json.dumps(row))
