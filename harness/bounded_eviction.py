"""Bound cooperative eviction to resident pages within SQLite's private mapping."""
from array import array
import ctypes
from heapq import nsmallest
from itertools import islice
import mmap
import time


RECENT_NS = 100_000_000


class BoundedEviction:
    def __init__(self, backing, address, libc, protect_recent=True, guard_ns=RECENT_NS, lru=False):
        if lru and not protect_recent:
            raise ValueError('LRU selection requires read history')
        self.lru = lru
        self.followup = None
        self.guard_ns = guard_ns
        self.backing = backing
        self.address = address
        self.libc = libc
        self.last_read = array("Q", [0]) * (len(backing) // mmap.PAGESIZE) if protect_recent else None
        self.early = set()
        self.advised = {}
        self.revisited = set()
        self.stats = dict(recent_skips=0, requests=0, immediately_nonresident=0, reused_within_100ms=0,
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
        cutoff = time.monotonic_ns() - self.guard_ns
        def eligible_pages():
            for i, resident in enumerate(bits, start):
                if resident & 1 and i not in self.advised:
                    if self.last_read is not None and self.last_read[i] >= cutoff:
                        self.stats["recent_skips"] += 1
                        continue
                    yield i

        eligible = eligible_pages()
        if self.lru:
            # Zero timestamps are unseen pages. nsmallest preserves address order on ties.
            victims = nsmallest(budget, eligible, key=self.last_read.__getitem__)
        else:
            victims = islice(eligible, budget)
        for i in victims:
            if self.followup:
                self.followup.before(i)
            self.backing.madvise(getattr(mmap, "MADV_PAGEOUT", 21), i * page, page)
            self.advised[i] = time.monotonic_ns()
            self.stats["requests"] += 1
            resident = (self.followup.immediate(i, self.advised[i]) if self.followup
                        else self.residency(i * page, 1)[0] & 1)
            self.stats["immediately_nonresident"] += int(not resident)

    def on_read(self, offset, amount):
        now = time.monotonic_ns() if self.last_read is not None else None
        for i in range(offset // mmap.PAGESIZE, (offset + amount - 1) // mmap.PAGESIZE + 1):
            if self.last_read is not None:
                self.last_read[i] = now
            if i in self.advised and i not in self.revisited:
                self.revisited.add(i)
                if time.monotonic_ns() - self.advised[i] <= RECENT_NS:
                    self.early.add(i)
                    self.stats["reused_within_100ms"] += 1
                    self.stats["nonresident_on_early_reuse"] += int((self.residency(i * mmap.PAGESIZE, 1)[0] & 1) == 0)

    def finish(self, end_ns):
        complete = {i for i, stamp in self.advised.items() if end_ns - stamp >= RECENT_NS}
        self.stats["full_window_pages"] = len(complete)
        self.stats["full_window_early_reuses"] = len(complete & self.early)
