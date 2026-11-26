"""Time-aligned mincore and pagemap observations for bounded eviction diagnostics."""
import heapq
import mmap
import os
import struct
import time


class ResidencyFollowup:
    def __init__(self, selector):
        self.selector = selector
        self.fd = os.open("/proc/self/pagemap", os.O_RDONLY | os.O_CLOEXEC)
        self.pending = []
        self.pages = {}

    def sample(self, page):
        selector = self.selector
        present = bool(selector.residency(page * mmap.PAGESIZE, 1)[0] & 1)
        offset = (selector.address // mmap.PAGESIZE + page) * 8
        bits, = struct.unpack("=Q", os.pread(self.fd, 8, offset))
        state = "present" if bits & (1 << 63) else "swapped" if bits & (1 << 62) else "unmapped"
        return dict(mincore_resident=present, pte=state, reread=page in selector.revisited)

    def before(self, page):
        stamp = self.selector.last_read[page]
        self.pages[page] = dict(last_read_age_ms=(time.monotonic_ns() - stamp) / 1e6 if stamp else None,
                                before=self.sample(page))

    def immediate(self, page, when):
        record = self.sample(page)
        record["delay_us"] = (time.monotonic_ns() - when) / 1000
        self.pages[page]["immediate"] = record
        for ms in (10, 100):
            heapq.heappush(self.pending, (when + ms * 1_000_000, page, ms))
        return record["mincore_resident"]

    def poll(self, now):
        while self.pending and self.pending[0][0] <= now:
            _, page, ms = heapq.heappop(self.pending)
            record = self.sample(page)
            record["delay_us"] = (time.monotonic_ns() - self.selector.advised[page]) / 1000
            self.pages[page][str(ms)] = record

    def close(self):
        os.close(self.fd)
