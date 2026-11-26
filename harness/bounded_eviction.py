"""Bound cooperative eviction to resident pages within SQLite's private mapping."""
import ctypes
import mmap
import time


class BoundedEviction:
    def __init__(self, backing, address, libc):
        self.backing = backing
        self.address = address
        self.libc = libc
        self.advised = {}
        self.revisited = set()
        self.stats = dict(requests=0, immediately_nonresident=0, reused_within_100ms=0,
                          nonresident_on_early_reuse=0)

    def residency(self, offset, pages):
        bits = (ctypes.c_ubyte * pages)()
        if self.libc.mincore(ctypes.c_void_p(self.address + offset), pages * mmap.PAGESIZE, bits):
            raise OSError(ctypes.get_errno(), "mincore")
        return bits

    def apply(self, region):
        low, high = region
        page = mmap.PAGESIZE
        if not self.address <= low < high <= self.address + len(self.backing):
            raise ValueError("candidate outside SQLite mapping")
        start = (low - self.address + page - 1) // page
        end = (high - self.address) // page
        if end <= start or len(self.advised) >= 64:
            return
        bits = self.residency(start * page, end - start)
        budget = min(16, 64 - len(self.advised))
        for i, resident in enumerate(bits, start):
            if resident & 1 and i not in self.advised:
                self.backing.madvise(mmap.MADV_PAGEOUT, i * page, page)
                self.advised[i] = time.monotonic_ns()
                self.stats["requests"] += 1
                self.stats["immediately_nonresident"] += int(not self.residency(i * page, 1)[0] & 1)
                budget -= 1
                if not budget:
                    break

    def on_read(self, offset, amount):
        for i in range(offset // mmap.PAGESIZE, (offset + amount - 1) // mmap.PAGESIZE + 1):
            if i in self.advised and i not in self.revisited:
                self.revisited.add(i)
                if time.monotonic_ns() - self.advised[i] <= 100_000_000:
                    self.stats["reused_within_100ms"] += 1
                    self.stats["nonresident_on_early_reuse"] += int(not self.residency(i * mmap.PAGESIZE, 1)[0] & 1)
