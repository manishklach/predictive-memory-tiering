"""Compare stock reclaim and pmtd detector-only on SQLite point queries under memory.max."""
import argparse
import ctypes
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


def worker(db, mode, binary):
    profiled = binary is not None
    size = db.stat().st_size
    pages = size // PAGE
    backing = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    with db.open("rb", buffering=0) as src:
        assert src.readinto(backing) == size
    address = ctypes.addressof(ctypes.c_char.from_buffer(backing))
    libc = ctypes.CDLL(None, use_errno=True)
    bitmap = ctypes.create_string_buffer(pages)
    reads = evictions = 0
    profile = dict(residency=0, controller_round_trip=0, pageout=0, scoring=0, pipe_path=0, inline_path=0)
    ctl = None
    inline = mode == "detector-inline"
    if inline:
        lib = ctypes.CDLL(str(ROOT / "libpmt-profile.so"))
        ptr = ctypes.c_void_p
        lib.pmt_policy_create.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
                                          ctypes.c_uint64, ctypes.c_int]
        lib.pmt_policy_create.restype = ptr
        lib.pmt_profile_step.argtypes = [ptr, ptr, ptr, ptr]
        lib.pmt_profile_step.restype = ctypes.c_uint64
        lib.pmt_policy_destroy.argtypes = [ptr]
        lib.pmt_policy_destroy.restype = None
        policy = lib.pmt_policy_create(pages, pages // 2, 10_000_000, 1_000_000, 0)
        if not policy:
            raise MemoryError("allocate inline policy")
        observation = ctypes.create_string_buffer(16)
        advice = (ctypes.c_uint32 * 2)()
    elif mode != "stock":
        ctl = subprocess.Popen([(binary or ROOT / "pmtd"), "--control", str(pages), str(pages // 2),
                                "10", "1", "none"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    base = apsw.vfsnames()[0]

    class File(apsw.VFSFile):
        def xRead(self, amount, offset):
            nonlocal reads, evictions
            data = backing[offset:offset + amount]
            for page in range(offset // PAGE, (offset + amount + PAGE - 1) // PAGE):
                reads += 1
                if ctl or inline:
                    t = time.perf_counter_ns()
                    refresh = mode == "detector-full" or (reads - 1) % 16 == 0
                    if refresh and libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(size), bitmap):
                        raise OSError(ctypes.get_errno(), "mincore")
                    bitmap[page] = 1  # The just-read page was resident at access.
                    now = time.perf_counter_ns()
                    profile['residency'] += now - t
                    if inline:
                        struct.pack_into("=QII", observation, 0, time.monotonic_ns(), page, 0)
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
    rng = random.Random(12345)
    keys = [rng.randrange(ROWS if i % 2 else 64) for i in range(REQUESTS)]
    times = []
    checksum = 0
    for stage in profile:
        profile[stage] = 0
    start = time.perf_counter_ns()
    for key in keys:
        t = time.perf_counter_ns()
        row = conn.execute("select id,length(payload) from items where id=?", (key,)).fetchone()
        times.append(time.perf_counter_ns() - t)
        if row != (key, 1500):
            raise ValueError("incorrect query result")
        checksum += row[0]
    elapsed = time.perf_counter_ns() - start
    conn.close()
    vfs.unregister()
    if inline:
        lib.pmt_policy_destroy(policy)
    if ctl:
        ctl.stdin.close()
        if ctl.wait():
            raise RuntimeError("controller failed")
    print(json.dumps(dict(p99_us=sorted(times)[(99 * REQUESTS + 99) // 100 - 1] / 1000,
                          queries_per_s=REQUESTS * 1e9 / elapsed, elapsed_ns=elapsed,
                          profile_ns=profile, reads=reads, pageout_requests=evictions, checksum=checksum)))


def run(db, mode, limit, binary=None):
    group = Path(tempfile.mkdtemp(prefix="pmt-sql-", dir="/sys/fs/cgroup"))
    try:
        if limit:
            (group / "memory.max").write_text(str(limit))
        def enter():
            (group / "cgroup.procs").write_text(str(os.getpid()))
        raw = subprocess.check_output([sys.executable, __file__, "--worker", db, mode, *([binary] if binary else [])],
                                      preexec_fn=enter)
        return dict(mode=mode, **json.loads(raw), peak_bytes=int((group / "memory.peak").read_text()),
                    swap_peak_bytes=int((group / "memory.swap.peak").read_text()),
                    memory_events=(group / "memory.events").read_text().splitlines())
    finally:
        group.rmdir()


def main(output, baseline):
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "app.db"
        setup = sqlite3.connect(db)
        setup.execute("pragma page_size=4096")
        setup.execute("create table items(id integer primary key, payload blob)")
        setup.executemany("insert into items values (?,zeroblob(1500))", ((i,) for i in range(ROWS)))
        setup.commit()
        setup.close()
        control = run(db, "stock", None)
        if control["swap_peak_bytes"]:
            raise ValueError("calibration swapped")
        limit = control["peak_bytes"] // (2 * PAGE) * PAGE
        runs = []
        before, after = ("detector-before", "detector-inline") if baseline else ("detector-full", "detector")
        orders = (("stock", before, after), (after, before, "stock"),
                  (before, "stock", after), (after, "stock", before))
        for pair, order in enumerate(orders):
            for mode in order:
                binary = baseline if mode == "detector-before" else None
                record = run(db, mode, limit, binary)
                assert record["checksum"] == control["checksum"]
                runs.append(dict(pair=pair + 1, **record))
        summary = {mode: {metric: statistics.median(r[metric] for r in runs if r["mode"] == mode)
                          for metric in ("p99_us", "queries_per_s")} for mode in orders[0]}
        output.write_text(json.dumps(dict(kernel=platform.release(), sqlite=apsw.sqlitelibversion(),
                             apsw=apsw.apswversion(), pages=db.stat().st_size // PAGE, requests=REQUESTS,
                             controller_sha256=hashlib.sha256((ROOT / "pmtd").read_bytes()).hexdigest(),
                             profile_binary_sha256={str(label): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for label, path in ([("before", baseline), ("after", ROOT / "libpmt-profile.so")]
                                                     if baseline else [])},
                             calibration=control, memory_max_bytes=limit, runs=runs, median=summary),
                             indent=2) + "\n")
        print(summary)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        worker(Path(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("output", type=Path)
        parser.add_argument("--profile-baseline", type=Path, help="prior instrumented pmtd binary")
        args = parser.parse_args()
        main(args.output, args.profile_baseline)
