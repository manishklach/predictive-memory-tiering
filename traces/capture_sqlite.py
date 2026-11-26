"""Measure SQLite main-file reads during periodic scans and hot-key requests."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time

import apsw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--period-seconds", type=int, default=12)
    parser.add_argument("--cycles", type=int, default=2)
    args = parser.parse_args()
    if args.period_seconds < 1 or args.cycles < 1:
        parser.error("period and cycles must be positive")
    last = {}
    reused = {seconds: set() for seconds in (10, 60, 300)}
    reads = 0
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, args.trace.open("w", newline="") as out:
        db = str(Path(directory) / "work.db")
        with sqlite3.connect(db) as setup:
            setup.execute("pragma page_size=4096")
            setup.execute("create table items(id integer primary key, payload blob)")
            setup.executemany("insert into items values (?, zeroblob(1500))",
                              ((i,) for i in range(512)))
        setup.close()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(("timestamp_ns", "page", "op"))
        start = time.monotonic_ns()
        base = apsw.vfs_names()[0]

        class File(apsw.VFSFile):
            def xRead(self, amount, offset):
                nonlocal reads
                data = super().xRead(amount, offset)
                now = time.monotonic_ns() - start
                for page in range(offset // 4096, (offset + amount + 4095) // 4096):
                    if page in last:
                        for seconds, pages in reused.items():
                            if now - last[page] >= seconds * 10**9:
                                pages.add(page)
                    last[page] = now
                    writer.writerow((now, page, "R"))
                    reads += 1
                return data

        class VFS(apsw.VFS):
            def xOpen(self, name, flags):
                if flags[0] & apsw.SQLITE_OPEN_MAIN_DB:
                    return File(base, name, flags)
                return super().xOpen(name, flags)

        vfs = VFS("pmt-capture", base)
        conn = apsw.Connection(db, flags=apsw.SQLITE_OPEN_READONLY, vfs="pmt-capture")
        conn.execute("pragma mmap_size=0")
        conn.execute("pragma cache_size=8")
        for cycle in range(args.cycles + 1):
            assert conn.execute("select sum(length(payload)) from items").fetchone()[0] == 768000
            if cycle < args.cycles:
                end = time.monotonic() + args.period_seconds
                request = 0
                while time.monotonic() < end:
                    conn.execute("select payload from items where id=?", (request % 32,)).fetchone()
                    request += 1
                    time.sleep(.02)
        conn.close()
        vfs.unregister()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "scope": "SQLite main-database VFS reads, synthetic data and scheduled scans",
        "sqlite_version": apsw.sqlitelibversion(), "apsw_version": apsw.apswversion(),
        "period_seconds": args.period_seconds, "cycles": args.cycles,
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "reads": reads, "unique_pages": len(last), "duration_ns": max(last.values()),
        "pages_reused_after_gap_seconds": {k: len(v) for k, v in reused.items()},
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
