"""Bound cooperative eviction to resident pages within SQLite's private mapping."""
from array import array
from contextlib import nullcontext
import ctypes
from heapq import nsmallest
from itertools import islice
import mmap
import time


RECENT_NS = 100_000_000


class BoundedEviction:
    def __init__(self, backing, address, libc, protect_recent=True, guard_ns=RECENT_NS, lru=False, skip_swapped=False):
        self.skip_swapped = skip_swapped
        if lru and not protect_recent:
            raise ValueError('LRU selection requires read history')
        self.lru = lru
        self.followup = None
        self.capture = None
        self.predictor = None
        self.swap_trace = None
        self.idle_risk = None
        self.guard_ns = guard_ns
        self.backing = backing
        self.address = address
        self.libc = libc
        self.last_read = array("Q", [0]) * (len(backing) // mmap.PAGESIZE) if protect_recent else None
        self.early = set()
        self.advised = {}
        self.revisited = set()
        self.stats = dict(swap_skips=0, recent_skips=0, requests=0, immediately_nonresident=0, reused_within_100ms=0,
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
        if self.capture:
            eligible = list(eligible)
            snapshot = self.capture.epoch(self, eligible, budget)
        if self.predictor and self.capture:
            def snapshot_risk(row):
                i, stamp, pte = row
                features = dict(page=i, last_read_ns=stamp, pte=pte)
                return self.predictor.score(snapshot, features), i
            victims = [r[0] for r in nsmallest(budget, snapshot['candidates'], key=snapshot_risk)]
        elif self.predictor:
            from residency_followup import pagemap_state
            epoch = {"ns": time.monotonic_ns()}
            with open("/proc/self/pagemap", "rb") as pagemap:
                def risk(i):
                    row = dict(page=i, last_read_ns=self.last_read[i],
                               pte=pagemap_state(pagemap.fileno(), self.address, i))
                    return self.predictor.score(epoch, row), i
                victims = nsmallest(budget, eligible, key=risk)
        elif self.skip_swapped:
            # Walk beyond the top K to refill slots rejected by the last-moment PTE check.
            victims = sorted(eligible, key=self.last_read.__getitem__)
        elif self.idle_risk is not None:
            now = time.monotonic_ns()
            def idle_score(i):
                stamp = self.last_read[i]
                age = ((now - stamp) // RECENT_NS).bit_length() if stamp else -1
                return self.idle_risk.get(str(age), self.idle_risk["fallback"]), i
            victims = nsmallest(budget, eligible, key=idle_score)
        elif self.lru:
            # Zero timestamps are unseen pages. nsmallest preserves address order on ties.
            victims = nsmallest(budget, eligible, key=self.last_read.__getitem__)
        else:
            victims = islice(eligible, budget)
        if self.capture:
            victims = list(victims)
            self.capture.record(snapshot, victims)
        with (open("/proc/self/pagemap", "rb") if self.skip_swapped and not self.followup
              else nullcontext()) as pagemap:
            for i in victims:
                if self.followup:
                    if not self.followup.before(i, self.skip_swapped):
                        self.stats["swap_skips"] += 1
                        continue
                elif pagemap is not None:
                    from residency_followup import pagemap_state
                    if pagemap_state(pagemap.fileno(), self.address, i) == "swapped":
                        self.stats["swap_skips"] += 1
                        continue
                self.backing.madvise(getattr(mmap, "MADV_PAGEOUT", 21), i * page, page)
                self.advised[i] = time.monotonic_ns()
                if self.swap_trace:
                    self.swap_trace.mark(self.address + i * page, i)
                self.stats["requests"] += 1
                resident = (self.followup.immediate(i, self.advised[i]) if self.followup
                            else self.residency(i * page, 1)[0] & 1)
                self.stats["immediately_nonresident"] += int(not resident)
                budget -= 1
                if budget == 0:
                    break

    def on_read(self, offset, amount):
        now = time.monotonic_ns() if self.last_read is not None else None
        if self.capture:
            self.capture.read(offset, amount, now)
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
