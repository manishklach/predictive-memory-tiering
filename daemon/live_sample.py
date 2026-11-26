"""Observe one Linux process with DAMON vaddr and a private aggregation trace."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import select
import signal
import tempfile
import time

ADMIN = Path("/sys/kernel/mm/damon/admin/kdamonds")
TRACE = Path("/sys/kernel/tracing/instances")
EVENT = re.compile(r"damon_aggregated: target_id=0 nr_regions=\d+ (\d+)-(\d+): (\d+) (\d+)")


@contextmanager
def sampling(pid, ranges=()):
    """Yield a record list, populated on exit. Kernel pins its target PID."""
    pidfd = os.pidfd_open(pid)
    trace = None
    configured = False
    records = []
    daemon = ADMIN / "0"
    ctx = daemon / "contexts/0"
    alive = select.poll()
    alive.register(pidfd, select.POLLIN)
    try:
        if (ADMIN / "nr_kdamonds").read_text().strip() != "0":
            raise RuntimeError("DAMON interface is already in use")
        trace = Path(tempfile.mkdtemp(prefix="pmt-", dir=TRACE))
        (trace / "tracing_on").write_text("0")
        (trace / "buffer_size_kb").write_text("256")
        (ADMIN / "nr_kdamonds").write_text("1")
        configured = True
        (daemon / "contexts/nr_contexts").write_text("1")
        (ctx / "operations").write_text("vaddr")
        (ctx / "targets/nr_targets").write_text("1")
        target = ctx / "targets/0"
        (target / "pid_target").write_text(str(pid))
        if ranges:
            (target / "regions/nr_regions").write_text(str(len(ranges)))
            for i, (start, end) in enumerate(ranges):
                region = target / f"regions/{i}"
                (region / "start").write_text(str(start))
                (region / "end").write_text(str(end))
        intervals = ctx / "monitoring_attrs/intervals"
        for key, value in (("sample_us", 5000), ("aggr_us", 100000), ("update_us", 0 if ranges else 1000000)):
            (intervals / key).write_text(str(value))
        if (ctx / "schemes/nr_schemes").read_text().strip() != "0":
            raise RuntimeError("expected observation without schemes")
        (daemon / "state").write_text("on")
        if alive.poll(0):
            raise RuntimeError("process exited during attach")
        kpid = int((daemon / "pid").read_text())
        event = trace / "events/damon/damon_aggregated"
        (event / "filter").write_text(f"common_pid == {kpid}")
        (event / "enable").write_text("1")
        (trace / "tracing_on").write_text("1")
        yield records
        (trace / "tracing_on").write_text("0")
        for path in (trace / "per_cpu").glob("cpu*/stats"):
            if re.search(r"^overrun:\s*[1-9]", path.read_text(), re.M):
                raise RuntimeError("PMT trace overrun")
        for line in (trace / "trace").read_text().splitlines():
            match = EVENT.search(line)
            if match:
                records.append(tuple(map(int, match.groups())))
    finally:
        try:
            if configured:
                if (daemon / "state").read_text().strip() == "on":
                    (daemon / "state").write_text("off")
                (ADMIN / "nr_kdamonds").write_text("0")
        finally:
            os.close(pidfd)
            if trace:
                trace.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("--seconds", type=float, default=5)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: raise_exit())
    with sampling(args.pid) as records:
        end = time.monotonic() + args.seconds
        with open(f"/proc/{args.pid}/stat") as proc:
            while time.monotonic() < end:
                proc.seek(0)
                try:
                    if not proc.read():
                        break
                except ProcessLookupError:
                    break
                time.sleep(.05)
    print(json.dumps(dict(pid=args.pid, fields=["start", "end", "sampled_accesses", "age"],
                          records=records)))


def raise_exit():
    raise SystemExit(0)


if __name__ == "__main__":
    main()
