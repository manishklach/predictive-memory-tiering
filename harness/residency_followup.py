"""Time-aligned mincore and pagemap observations for bounded eviction diagnostics."""
import heapq
import mmap
import os
import struct
import time


def pagemap_state(fd, address, page):
    offset = (address // mmap.PAGESIZE + page) * 8
    bits, = struct.unpack("=Q", os.pread(fd, 8, offset))
    return "present" if bits & (1 << 63) else "swapped" if bits & (1 << 62) else "unmapped"


class ResidencyFollowup:
    def __init__(self, selector):
        self.selector = selector
        self.fd = os.open("/proc/self/pagemap", os.O_RDONLY | os.O_CLOEXEC)
        self.pending = []
        self.confirmed = 0
        self.outstanding = 0
        self.pages = {}

    def sample(self, page):
        selector = self.selector
        present = bool(selector.residency(page * mmap.PAGESIZE, 1)[0] & 1)
        state = pagemap_state(self.fd, selector.address, page)
        return dict(mincore_resident=present, pte=state, reread=page in selector.revisited)

    def before(self, page, skip_swapped=False):
        record = self.sample(page)
        if skip_swapped and record["pte"] == "swapped":
            return False
        stamp = self.selector.last_read[page]
        now = time.monotonic_ns()
        self.pages[page] = dict(last_read_age_ms=(now - stamp) / 1e6 if stamp else None,
                                before=record)
        if self.selector.read_history is not None:
            self.pages[page]["recent_reads"] = self.selector.read_history.count(page, now)
        return True

    def immediate(self, page, when):
        record = self.sample(page)
        record["delay_us"] = (time.monotonic_ns() - when) / 1000
        self.pages[page]["immediate"] = record
        self.outstanding += 1
        for ms in (10, 100):
            heapq.heappush(self.pending, (when + ms * 1_000_000, page, ms))
        return record["mincore_resident"]

    def poll(self, now):
        while self.pending and self.pending[0][0] <= now:
            _, page, ms = heapq.heappop(self.pending)
            record = self.sample(page)
            record["delay_us"] = (time.monotonic_ns() - self.selector.advised[page]) / 1000
            self.pages[page][str(ms)] = record
            if ms == 10:
                self.outstanding -= 1
                before = self.pages[page]["before"]
                if before["mincore_resident"] and before["pte"] == "present" and not record["mincore_resident"]:
                    self.confirmed += 1

    def close(self):
        os.close(self.fd)
