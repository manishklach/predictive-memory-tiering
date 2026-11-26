"""Exercise one predicted return on disposable private anonymous Linux memory."""

import argparse
import ctypes
import json
import mmap
import os
import struct
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pte-prefault", action="store_true",
                        help="test synchronous PTE restoration, including swap-cache hits")
    args = parser.parse_args()
    size = os.sysconf("SC_PAGE_SIZE")
    if size != 4096:
        raise SystemExit("requires 4 KiB pages")
    libc = ctypes.CDLL(None, use_errno=True)
    none = 2**32 - 1
    # Batch the rehearsal page with adjacent cold pages for clustered swap I/O.
    with mmap.mmap(-1, 256 * size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS) as arena:
        address = ctypes.addressof(ctypes.c_char.from_buffer(arena))

        def residency():
            bits = (ctypes.c_ubyte * 4)()
            if libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(4 * size), bits):
                raise OSError(ctypes.get_errno(), "mincore")
            return bytes(value & 1 for value in bits)

        def pte(page):
            offset = ((address // size) + page) * 8
            with open("/proc/self/pagemap", "rb", buffering=0) as pagemap:
                pagemap.seek(offset)
                return struct.unpack("=Q", pagemap.read(8))[0]

        def swapped(page):
            return bool(pte(page) & (1 << 62))

        def observed():
            if args.pte_prefault:
                return bytes(pte(page) >> 63 for page in range(4))
            return residency()

        arena.madvise(mmap.MADV_NOHUGEPAGE)
        data = os.urandom(size)
        for page in range(256):
            arena[page * size:(page + 1) * size] = data
        with subprocess.Popen(["./pmtd", "--control", "4", "4", "1000", "100", "markov"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as controller:
            def observe(page):
                assert arena[page * size] == data[0]
                controller.stdin.write(struct.pack("=QII", time.monotonic_ns(), page, 0)
                                       + observed())
                controller.stdin.flush()
                return struct.unpack("=II", controller.stdout.read(8))

            try:
                for page in (0, 3, 1, 0, 3, 1):
                    observe(page)
                # Induce a known swap-out to isolate the predictor and return path.
                for _ in range(100):
                    # Linux UAPI value; some CPython builds omit the symbolic constant.
                    arena.madvise(getattr(mmap, "MADV_PAGEOUT", 21), size, 255 * size)
                    time.sleep(.02)
                    if swapped(1) and not observed()[1]:
                        break
                else:
                    raise SystemExit(f"inconclusive: target swapped={swapped(1)}, "
                                     f"mincore={residency()[1]} after page-out attempts")
                observe(0)
                eviction, predicted = observe(3)
                assert (eviction, predicted) == (none, 1)
                assert swapped(predicted) and not observed()[predicted]
                cached = bool(residency()[predicted])
                advice = getattr(mmap, "MADV_POPULATE_READ", 22) if args.pte_prefault else mmap.MADV_WILLNEED
                arena.madvise(advice, predicted * size, size)
                for _ in range(100):
                    if observed()[predicted]:
                        break
                    time.sleep(.01)
                else:
                    raise SystemExit("inconclusive: no resident page observed before demand")
                pte_before_demand = pte(predicted) >> 63
                assert arena[size:2 * size] == data
                print(json.dumps({"scope": "cooperative correctness fixture",
                                  "kernel": os.uname().release, "predicted_page": predicted,
                                  "advice": "POPULATE_READ" if args.pte_prefault else "WILLNEED",
                                  "mincore_before_advice": cached,
                                  "pte_present_before_demand": bool(pte_before_demand),
                                  "swapped_before_advice": True, "resident_before_demand": True,
                                  "payload_verified_bytes": size}, indent=2))
            finally:
                controller.stdin.close()
            if controller.wait():
                raise SystemExit("controller failed")


if __name__ == "__main__":
    main()
