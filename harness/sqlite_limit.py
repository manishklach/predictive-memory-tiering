"""Compare stock reclaim and pmtd detector-only on SQLite point queries under memory.max."""
import resource
from array import array
import argparse
from contextlib import nullcontext
from itertools import permutations
import ctypes
from check_damos import pageout, scheme_stats
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import random
import sqlite3
import statistics
import struct
import subprocess
import sys
import tempfile
import time

import apsw

PAGE = 4096
ROWS = 14000
REQUESTS = 3000
ROOT = Path(__file__).resolve().parents[1]


sys.path.insert(0, str(ROOT / "daemon"))
from live_sample import sampling
from region_detector import RegionDetector


def worker(db, mode, binary, interval, score_interval, seed, requests=REQUESTS):
    profiled = binary is not None
    size = db.stat().st_size
    pages = size // PAGE
    backing = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    with db.open("rb", buffering=0) as src:
        assert src.readinto(backing) == size
    address = ctypes.addressof(ctypes.c_char.from_buffer(backing))
    libc = ctypes.CDLL(None, use_errno=True)
    bitmap = ctypes.create_string_buffer(pages)
    reads = evictions = eviction_advice = refreshes = 0
    profile = dict(residency=0, controller_round_trip=0, pageout=0, scoring=0, pipe_path=0, inline_path=0)
    score_calls = history_only_calls = 0
    ctl = None
    inline = mode in ("scoring-only", "detector-inline")
    bounded = None
    if mode in ("bounded-on", "bounded-legacy", "bounded-300ms", "bounded-lru"):
        from bounded_eviction import BoundedEviction
        bounded = BoundedEviction(backing, address, libc, protect_recent=mode != "bounded-legacy",
                                  guard_ns=300_000_000 if mode == "bounded-300ms" else 100_000_000, lru=mode == "bounded-lru")
    followup = None
    if bounded and os.environ.get("PMT_RESIDENCY_FOLLOWUP") == "1":
        from residency_followup import ResidencyFollowup
        followup = ResidencyFollowup(bounded)
        bounded.followup = followup
    observe = mode not in ("stock", "damos", "live", "live-score", "bounded-off", "bounded-on", "bounded-legacy", "bounded-300ms", "bounded-lru")
    if inline:
        lib = ctypes.CDLL(str(ROOT / "libpmt-profile.so"))
        ptr = ctypes.c_void_p
        lib.pmt_policy_create.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
                                          ctypes.c_uint64, ctypes.c_int]
        lib.pmt_policy_create.restype = ptr
        lib.pmt_detector_observe.argtypes = [ptr, ptr]
        lib.pmt_detector_observe.restype = None
        lib.pmt_profile_step.argtypes = [ptr, ptr, ptr, ptr]
        lib.pmt_profile_step.restype = ctypes.c_uint64
        lib.pmt_policy_destroy.argtypes = [ptr]
        lib.pmt_policy_destroy.restype = None
        policy = lib.pmt_policy_create(pages, pages // 2, 10_000_000, 1_000_000, 0)
        if not policy:
            raise MemoryError("allocate inline policy")
        observation = ctypes.create_string_buffer(16)
        advice = (ctypes.c_uint32 * 2)()
    elif observe and mode != "observation-only":
        ctl = subprocess.Popen([(binary or ROOT / "pmtd"), "--control", str(pages), str(pages // 2),
                                "10", "1", "none"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    base = apsw.vfsnames()[0]

    class File(apsw.VFSFile):
        def xRead(self, amount, offset):
            nonlocal score_calls, history_only_calls
            nonlocal reads, evictions, eviction_advice, refreshes
            if bounded:
                bounded.on_read(offset, amount)
            data = backing[offset:offset + amount]
            for page in range(offset // PAGE, (offset + amount + PAGE - 1) // PAGE):
                reads += 1
                if observe:
                    t = time.perf_counter_ns()
                    refresh = mode == "detector-full" or (reads - 1) % interval == 0
                    if refresh:
                        if libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(size), bitmap):
                            raise OSError(ctypes.get_errno(), "mincore")
                        refreshes += 1
                    bitmap[page] = 1  # The just-read page was resident at access.
                    now = time.perf_counter_ns()
                    profile['residency'] += now - t
                    if mode == "observation-only":
                        continue
                    if inline:
                        struct.pack_into("=QII", observation, 0, time.monotonic_ns(), page, 0)
                        if (reads - 1) % score_interval:
                            lib.pmt_detector_observe(policy, observation)
                            history_only_calls += 1
                            continue
                        score_calls += 1
                        scoring = lib.pmt_profile_step(policy, observation, bitmap, advice)
                        victim, fetch = advice
                    else:
                        ctl.stdin.write(struct.pack("=QII", time.monotonic_ns(), page, 0) + bitmap.raw)
                        ctl.stdin.flush()
                        if profiled:
                            victim, fetch, scoring = struct.unpack("=IIQ", ctl.stdout.read(16))
                        else:
                            victim, fetch = struct.unpack("=II", ctl.stdout.read(8))
                    replied = time.perf_counter_ns()
                    profile['controller_round_trip'] += replied - now
                    if profiled or inline:
                        profile["scoring"] += scoring
                        profile["inline_path" if inline else "pipe_path"] += replied - now - scoring
                    assert fetch == 0xffffffff
                    if victim != 0xffffffff:
                        eviction_advice += 1
                        if mode == "scoring-only":
                            continue
                        backing.madvise(getattr(mmap, "MADV_PAGEOUT", 21), victim * PAGE, PAGE)
                        if mode != "detector-full":
                            bitmap[victim] = 0  # Provisional until the next scan or touch.
                        profile['pageout'] += time.perf_counter_ns() - replied
                        evictions += 1
            return data

    class VFS(apsw.VFS):
        def xOpen(self, name, flags):
            if flags[0] & apsw.SQLITE_OPEN_MAIN_DB:
                return File(base, name, flags)
            return super().xOpen(name, flags)

    vfs = VFS("pmt-bench", base)
    conn = apsw.Connection(str(db), flags=apsw.SQLITE_OPEN_READONLY, vfs="pmt-bench")
    conn.execute("pragma mmap_size=0")
    conn.execute("pragma cache_size=8")
    rng = random.Random(seed)
    draws = (rng.randrange(ROWS if i % 2 else 64) for i in range(requests))
    # Compact long-run bookkeeping under the same workload memory cap.
    keys = array("I", draws) if requests > REQUESTS else list(draws)
    times = array("Q") if requests > REQUESTS else []
    checksum = 0
    for stage in profile:
        profile[stage] = 0
    stats = None
    detector = RegionDetector() if mode in ("live-score", "bounded-off", "bounded-on", "bounded-legacy", "bounded-300ms", "bounded-lru") else None
    context = nullcontext()
    if mode == "damos":
        context = pageout(os.getpid(), address, size)
    elif mode in ("bounded-off", "bounded-on", "bounded-legacy", "bounded-300ms", "bounded-lru"):
        ranges = [(address, address + size)]
        context = sampling(os.getpid(), ranges, detector, buffer_kb=32)
    elif mode in ("live", "live-score"):
        context = sampling(os.getpid(), detector=detector, buffer_kb=32)
    fault_probe = None
    if os.environ.get("PMT_FAULT_PROFILE") == "1":
        from fault_profile import FaultProfile
        fault_probe = FaultProfile()
    with context as monitor:
        if fault_probe:
            memory_at_start = fault_probe.memory_breakdown()
            fault_start = fault_probe.snapshot()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        start = time.perf_counter_ns()
        consumed = 0
        for key in keys:
            t = time.perf_counter_ns()
            if followup:
                followup.poll(t)
            if bounded and consumed < len(detector.decisions):
                consumed = len(detector.decisions)
                region = detector.decisions[consumed - 1][1]
                if region:
                    bounded.apply(region)
            row = conn.execute("select id,length(payload) from items where id=?", (key,)).fetchone()
            times.append(time.perf_counter_ns() - t)
            if row != (key, 1500):
                raise ValueError("incorrect query result")
            checksum += row[0]
        elapsed = time.perf_counter_ns() - start
        if bounded:
            bounded.finish(start + elapsed)
        if followup:
            followup.close()
        used = resource.getrusage(resource.RUSAGE_SELF)
        if fault_probe:
            fault_end = fault_probe.snapshot()
            fault_probe.sock.close()
        if detector:
            scored = [choice for stamp, choice in detector.decisions if start <= stamp <= start + elapsed]
        if mode == "damos":
            stats = scheme_stats(*monitor)
    if os.environ.get("PMT_BOUNDED_TRIAL") == "1":
        digest = hashlib.sha256(backing).hexdigest()
        if digest != os.environ["PMT_DB_SHA256"]:
            raise ValueError("database mapping changed")
    conn.close()
    vfs.unregister()
    if inline:
        lib.pmt_policy_destroy(policy)
    if ctl:
        ctl.stdin.close()
        if ctl.wait():
            raise RuntimeError("controller failed")
    print(json.dumps(dict(p99_us=sorted(times)[(99 * requests + 99) // 100 - 1] / 1000,
                          queries_per_s=requests * 1e9 / elapsed, elapsed_ns=elapsed,
                          worker_cpu_ms=1000*(used.ru_utime + used.ru_stime - usage.ru_utime - usage.ru_stime),
                          memory_at_query_start=memory_at_start if fault_probe else None,
                          fault_profile={k: v - fault_start[k] for k, v in fault_end.items()} if fault_probe else None,
                          minor_faults=used.ru_minflt - usage.ru_minflt,
                          major_faults=used.ru_majflt - usage.ru_majflt,
                          voluntary_switches=used.ru_nvcsw - usage.ru_nvcsw,
                          reader_profile=detector.profile if detector else None,
                          live_scored_epochs=len(scored) if detector else None,
                          live_candidate_epochs=sum(c is not None for c in scored) if detector else None,
                          live_region_records=len(monitor) if mode == "live" else None, damos_stats=stats, inline_score_calls=score_calls, history_only_calls=history_only_calls,
                          profile_ns=profile, reads=reads, residency_scans=refreshes, eviction_advice=eviction_advice, bounded_eviction=bounded.stats if bounded else None,
                          residency_followup=followup.pages if followup else None, pageout_requests=bounded.stats["requests"] if bounded else evictions, checksum=checksum)))


def run(db, mode, limit, binary=None, interval=16, score_interval=1, seed=12345, requests=REQUESTS):
    group = Path(tempfile.mkdtemp(prefix="pmt-sql-", dir="/sys/fs/cgroup"))
    try:
        if limit:
            (group / "memory.max").write_text(str(limit))
            if "PMT_ZSWAP_MAX_BYTES" in os.environ:
                (group / "memory.zswap.max").write_text(str(int(os.environ["PMT_ZSWAP_MAX_BYTES"])))
        def enter():
            (group / "cgroup.procs").write_text(str(os.getpid()))
        raw = subprocess.check_output([sys.executable, __file__, "--worker", db, mode, str(interval), str(score_interval), str(seed), *([binary] if binary else [])],
                                      preexec_fn=enter, env={**os.environ, 'PMT_WORKER_REQUESTS': str(requests)})
        return dict(mode=mode, **json.loads(raw), peak_bytes=int((group / "memory.peak").read_text()),
                    swap_peak_bytes=int((group / "memory.swap.peak").read_text()),
                    memory_events=(group / "memory.events").read_text().splitlines())
    finally:
        group.rmdir()


def main(output, baseline, stages, sweep, scoring_sweep, extended, damos, live, live_score, live_profile, paired_calibration, bounded_trial, selection_trial, scoring_trial, requests=REQUESTS, guard_trial=False, lru_trial=False):
    bounded_trial = bounded_trial or selection_trial or scoring_trial or guard_trial or lru_trial
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "app.db"
        setup = sqlite3.connect(db)
        setup.execute("pragma page_size=4096")
        setup.execute("create table items(id integer primary key, payload blob)")
        if bounded_trial:
            if Path("/sys/module/zswap/parameters/enabled").read_text().strip() != "N":
                raise RuntimeError("bounded trial requires zswap off")
            rng = random.Random(8471)
            setup.executemany("insert into items values (?,?)",
                              ((i, struct.pack("<Q", i) + rng.randbytes(1492)) for i in range(ROWS)))
        else:
            setup.executemany("insert into items values (?,zeroblob(1500))", ((i,) for i in range(ROWS)))
        setup.commit()
        setup.close()
        if bounded_trial:
            os.environ["PMT_BOUNDED_TRIAL"] = "1"
            os.environ["PMT_DB_SHA256"] = hashlib.sha256(db.read_bytes()).hexdigest()
        control = run(db, "stock", None, requests=requests)
        if control["swap_peak_bytes"]:
            raise ValueError("calibration swapped")
        limit = control["peak_bytes"] // (2 * PAGE) * PAGE
        if paired_calibration:
            previous = json.loads(paired_calibration.read_text())
            if previous["kernel"] != platform.release() or previous["pages"] != db.stat().st_size // PAGE:
                raise ValueError("paired calibration requires the same kernel and database size")
            limit = previous["calibration"]["peak_bytes"] // (2 * PAGE) * PAGE
            if limit != previous["memory_max_bytes"]:
                raise ValueError("reference cap differs from its half-memory calibration")
        runs = []
        before, after = ("detector-before", "detector-inline") if baseline else ("detector-full", "detector")
        orders = (("stock", before, after), (after, before, "stock"),
                  (before, "stock", after), (after, "stock", before))
        if stages:
            modes = ("stock", "observation-only", "scoring-only", "detector-inline")
            orders = [tuple(modes[(i + j) % 4] for j in (0, 1, 3, 2)) for i in range(4)]
        settings = {}
        if scoring_sweep or extended:
            settings["stock"] = ("stock", 256, 1)
            for cadence in ((64, 128, 256) if extended else (1, 4, 16, 64)):
                settings[f"score-every-{cadence}"] = ("detector-inline", 256, cadence)
        if sweep:
            settings["stock"] = ("stock", 16, 1)
            for mode in ("observation-only", "detector-inline"):
                for interval in (16, 64, 256):
                    settings[f"{mode}-{interval}"] = (mode, interval, 1)
        if settings:
            rng = random.Random(1207)
            orders = [rng.sample(list(settings), len(settings)) for _ in range(4)]
        if extended:
            modes = list(settings)
            orders = []
            for block in range(3):
                rng.shuffle(modes)
                orders.extend(tuple(modes[(i + j) % 4] for j in (0, 1, 3, 2)) for i in range(4))
        if damos:
            settings = {"stock": ("stock", 256, 1), "damos": ("damos", 256, 1),
                        "pmt-256": ("detector-inline", 256, 256)}
            orders = list(permutations(settings)) * 2
            random.Random(1207).shuffle(orders)
        if live or live_score:
            arm = "live-score" if live_score else "live"
            orders = [("stock", arm), (arm, "stock")] * 6
        if live_profile:
            orders = list(permutations(("stock", "live", "live-score"))) * 2
            random.Random(1207).shuffle(orders)
        if bounded_trial:
            orders = list(permutations(("stock", "bounded-off", "bounded-on"))) * 2
            random.Random(1207).shuffle(orders)
        if selection_trial or guard_trial:
            modes = ["stock", "bounded-off", "bounded-on", "bounded-300ms" if guard_trial else "bounded-legacy"]
            orders = []
            rng = random.Random(1207)
            for block in range(3):
                rng.shuffle(modes)
                orders.extend(tuple(modes[(i + j) % 4] for j in (0, 1, 3, 2)) for i in range(4))
        if scoring_trial:
            orders = [("stock", "bounded-off"), ("bounded-off", "stock")] * 6
        if lru_trial:
            orders = [("bounded-on", "bounded-lru"), ("bounded-lru", "bounded-on")] * 6
        for pair, order in enumerate(orders):
            seed = 12345 + pair if extended or damos or live or live_score or live_profile or bounded_trial else 12345
            expected_checksum = control["checksum"] if seed == 12345 else None
            for mode in order:
                binary = baseline if mode == "detector-before" else None
                worker_mode, interval, cadence = settings.get(mode, (mode, 16, 1))
                record = run(db, worker_mode, limit, binary, interval, cadence, seed, requests)
                record["mode"] = mode
                if scoring_sweep or extended or damos:
                    record["score_interval"] = None if mode in ("stock", "damos", "live", "live-score") else cadence
                if settings:
                    record["refresh_interval"] = None if mode in ("stock", "damos", "live", "live-score") else interval
                if expected_checksum is None:
                    expected_checksum = record["checksum"]
                assert record["checksum"] == expected_checksum
                record["query_seed"] = seed
                runs.append(dict(pair=pair + 1, **record))
        summary = {mode: {metric: statistics.median(r[metric] for r in runs if r["mode"] == mode)
                          for metric in ("p99_us", "queries_per_s")} for mode in orders[0]}
        output.write_text(json.dumps(dict(kernel=platform.release(), sqlite=apsw.sqlitelibversion(),
                             page_cluster=int(Path("/proc/sys/vm/page-cluster").read_text()),
                             zswap_cap_bytes=os.environ.get("PMT_ZSWAP_MAX_BYTES"),
                             zswap_enabled=Path("/sys/module/zswap/parameters/enabled").read_text().strip()
                                if Path("/sys/module/zswap/parameters/enabled").exists() else None,
                             apsw=apsw.apswversion(), pages=db.stat().st_size // PAGE, requests=requests,
                             controller_sha256=hashlib.sha256((ROOT / "pmtd").read_bytes()).hexdigest(),
                             profile_binary_sha256={str(label): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for label, path in ([("inline", ROOT / "libpmt-profile.so")] if stages or settings else
                                                     [("before", baseline), ("after", ROOT / "libpmt-profile.so")]
                                                     if baseline else [])},
                             calibration=control, memory_max_bytes=limit, runs=runs, median=summary),
                             indent=2) + "\n")
        print(summary)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[7] if len(sys.argv) > 7 else None, int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]),
               int(os.environ.get('PMT_WORKER_REQUESTS', REQUESTS)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("output", type=Path)
        parser.add_argument("--requests", type=int, default=REQUESTS, help="point queries per seed and arm")
        parser.add_argument("--paired-calibration", type=Path, help="reuse the half-memory cap from a previous experiment")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--scoring-trial", action="store_true", help="matched stock and fixed-range scoring-only runs with opaque SQLite payloads")
        group.add_argument("--lru-trial", action="store_true", help="compare address order and LRU within the selected region at a fixed 100 ms guard")
        group.add_argument("--guard-trial", action="store_true", help="compare 100 ms and 300 ms page selection with a fixed 100 ms reuse metric")
        group.add_argument("--selection-trial", action="store_true", help="add an unguarded eviction control to the bounded trial")
        group.add_argument("--bounded-trial", action="store_true", help="compare stock and live scoring with bounded page-out off/on")
        group.add_argument("--live-profile", action="store_true", help="profile stock, live sampling and live scoring across twelve matched seeds")
        group.add_argument("--live-score", action="store_true", help="rank live region samples during SQLite queries")
        group.add_argument("--live", action="store_true", help="time stock and PID-scoped observation over twelve matched seeds")
        group.add_argument("--damos", action="store_true", help="compare stock, DAMOS and PMT 256/256 across 12 seeds")
        group.add_argument("--extended-scoring", action="store_true", help="compare 64, 128 and 256 over 12 matched seeds")
        group.add_argument("--scoring-sweep", action="store_true", help="rank every 1, 4, 16 or 64 observations at residency interval 256")
        group.add_argument("--sampling-sweep", action="store_true", help="compare refresh intervals of 16, 64 and 256 observations")
        group.add_argument("--stages", action="store_true", help="compare observation, scoring and page-out")
        group.add_argument("--profile-baseline", type=Path, help="prior instrumented pmtd binary")
        args = parser.parse_args()
        if args.requests < 1:
            parser.error('--requests must be positive')
        main(args.output, args.profile_baseline, args.stages, args.sampling_sweep, args.scoring_sweep, args.extended_scoring, args.damos, args.live, args.live_score, args.live_profile, args.paired_calibration, args.bounded_trial, args.selection_trial, args.scoring_trial, args.requests, args.guard_trial, args.lru_trial)
