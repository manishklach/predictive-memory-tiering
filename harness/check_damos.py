"""Exercise PID-scoped DAMOS page-out on a disposable child's anonymous mapping."""
import argparse
import ctypes
import json
import mmap
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


ADMIN = Path("/sys/kernel/mm/damon/admin/kdamonds")
SIZE = 32 * 1024 * 1024
PAGE = os.sysconf("SC_PAGE_SIZE")


def child():
    with mmap.mmap(-1, SIZE, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS) as arena:
        for offset in range(0, SIZE, PAGE):
            arena[offset] = 73
        base = ctypes.addressof(ctypes.c_char.from_buffer(arena))
        print(base, flush=True)
        if sys.stdin.readline().strip() == "verify":
            assert all(arena[offset] == 73 for offset in range(0, SIZE, PAGE))
            print("verified", flush=True)


def probe():
    if (ADMIN / "nr_kdamonds").read_text().strip() != "0":
        raise RuntimeError("existing DAMON configuration in use")
    if len(Path("/proc/swaps").read_text().splitlines()) < 2:
        raise RuntimeError("page-out probe requires active swap")
    daemon = ADMIN / "0"
    ctx = daemon / "contexts/0"
    scheme = ctx / "schemes/0"
    with subprocess.Popen([sys.executable, __file__, "--child"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as target:
        try:
            base = int(target.stdout.readline())
            (ADMIN / "nr_kdamonds").write_text("1")
            for path, value in (
                (daemon / "contexts/nr_contexts", 1), (ctx / "operations", "vaddr"),
                (ctx / "targets/nr_targets", 1), (ctx / "targets/0/pid_target", target.pid),
                (ctx / "targets/0/regions/nr_regions", 1),
                (ctx / "targets/0/regions/0/start", base), (ctx / "targets/0/regions/0/end", base + SIZE),
                (ctx / "monitoring_attrs/intervals/sample_us", 5000),
                (ctx / "monitoring_attrs/intervals/aggr_us", 100000),
                (ctx / "monitoring_attrs/intervals/update_us", 0),
                (ctx / "schemes/nr_schemes", 1), (scheme / "action", "pageout"),
                (scheme / "access_pattern/sz/min", PAGE), (scheme / "access_pattern/sz/max", SIZE),
                (scheme / "access_pattern/nr_accesses/max", 0),
                (scheme / "access_pattern/age/min", 2), (daemon / "state", "on"),
            ):
                path.write_text(str(value))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                time.sleep(0.25)
                (daemon / "state").write_text("update_schemes_stats")
                stats = {k: int((scheme / "stats" / k).read_text())
                         for k in ("nr_tried", "sz_tried", "nr_applied", "sz_applied")}
                status = Path(f"/proc/{target.pid}/status").read_text().splitlines()
                swap_kib = int(next(s for s in status if s.startswith("VmSwap:")).split()[1])
                if stats["sz_applied"] > 0 and swap_kib > 0:
                    break
            else:
                raise RuntimeError(f"no confirmed page-out: {stats}, VmSwap={swap_kib} KiB")
            (daemon / "state").write_text("off")
            target.stdin.write("verify\n")
            target.stdin.flush()
            assert target.stdout.readline().strip() == "verified" and target.wait() == 0
            return dict(kernel=platform.release(), operations="vaddr", action="pageout",
                        mapping_bytes=SIZE, sample_us=5000, aggregate_us=100000,
                        stats=stats, target_swap_kib=swap_kib, contents_verified=True)
        finally:
            if (daemon / "state").exists() and (daemon / "state").read_text().strip() == "on":
                (daemon / "state").write_text("off")
            (ADMIN / "nr_kdamonds").write_text("0")
            if target.poll() is None:
                target.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    if parser.parse_args().child:
        child()
    else:
        print(json.dumps(probe(), indent=2))
