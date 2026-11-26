"""Replay demand events through the same compiled controller used by the Linux arena."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from replay import load_trace


def run(events, daemon, pages, budget, idle_ms, dwell_ms, model, audit=None):
    resident = bytearray(pages)
    seen = set()
    evictions = prefetches = misses = over_budget = 0
    command = [str(daemon.resolve()), "--control", str(pages), str(budget),
               str(idle_ms), str(dwell_ms), model]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        try:
            for event in events:
                page = event.page
                misses += page in seen and not resident[page]
                seen.add(page)
                resident[page] = 1
                frame = struct.pack("=QII", event.timestamp_ns, page, int(event.write)) + resident
                proc.stdin.write(frame)
                proc.stdin.flush()
                reply = proc.stdout.read(8)
                if len(reply) != 8:
                    raise RuntimeError("controller closed before returning a complete decision")
                evict, prefetch = struct.unpack("=II", reply)
                if audit is not None:
                    audit.append((frame, (evict, prefetch)))
                if evict != 2**32 - 1:
                    resident[evict] = 0
                    evictions += 1
                if prefetch != 2**32 - 1:
                    resident[prefetch] = 1
                    prefetches += 1
                over_budget += sum(resident) > budget
        finally:
            proc.stdin.close()
        if proc.wait():
            raise RuntimeError("controller failed")
    return dict(evictions=evictions, prefetches=prefetches, repeat_misses=misses,
                observations_above_budget_after_advice=over_budget)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--daemon", type=Path, default=Path("./pmtd"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    events = load_trace(args.trace)
    pages = max(e.page for e in events) + 1
    if pages > 8192:
        parser.error("controller supports page IDs 0 through 8191")
    budget = max(1, len({e.page for e in events}) // 2)
    report = {"mode": "compiled controller; instantaneous successful advice; soft target",
              "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
              "daemon_sha256": hashlib.sha256(args.daemon.read_bytes()).hexdigest(),
              "idle_ms": 10000, "dwell_ms": 1000, "budget_pages": budget,
              "runs": {model: run(events, args.daemon, pages, budget, 10000, 1000, model)
                       for model in ("none", "stride", "markov")}}
    args.out.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
