"""Rank complete DAMON aggregates online using the C PMT detector, without actuation."""
import ctypes
import os
from pathlib import Path
import re
import threading
import time

LINE = re.compile(rb"(\d+\.\d+): damon_aggregated: target_id=0 nr_regions=(\d+) (\d+)-(\d+): (\d+) \d+")


class RegionDetector:
    def __init__(self):
        lib = ctypes.CDLL(str(Path(__file__).resolve().parents[1] / "libpmt-profile.so"))
        lib.pmt_region_state_size.restype = ctypes.c_size_t
        self.size = lib.pmt_region_state_size()
        self.step = lib.pmt_region_sample
        self.step.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint32,
                              ctypes.c_uint64, ctypes.c_uint64]
        self.step.restype = ctypes.c_double
        self.previous = []
        self.decisions = []
        self.partial = []
        self.error = None
        self.stop = threading.Event()

    def consume(self, line):
        match = LINE.search(line)
        if not match:
            return
        stamp, count, start, end, hits = match.groups()
        count, start, end, hits = map(int, (count, start, end, hits))
        if self.partial and start < self.partial[-1][1]:
            self.partial.clear()
        self.partial.append((start, end, hits))
        if len(self.partial) != count:
            return
        now = int(float(stamp) * 1e9)
        states = []
        best = 0
        winner = None
        index = 0
        for low, high, accesses in self.partial:
            while index < len(self.previous) and self.previous[index][1] <= low:
                index += 1
            state = ctypes.create_string_buffer(self.size)
            if index < len(self.previous):
                old_low, old_high, old_state = self.previous[index]
                if old_low <= low and high <= old_high:
                    ctypes.memmove(state, old_state, self.size)
            score = self.step(state, now, accesses, 100_000_000, 50_000_000)
            if score > best:
                best, winner = score, (low, high)
            states.append((low, high, state))
        self.previous = states
        self.decisions.append((time.monotonic_ns(), winner))
        self.partial.clear()

    def start(self, trace):
        self.fd = os.open(trace / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK)
        self.thread = threading.Thread(target=self.read)
        self.thread.start()

    def read(self):
        pending = b""
        try:
            while not self.stop.is_set():
                try:
                    chunk = os.read(self.fd, 65536)
                except BlockingIOError:
                    self.stop.wait(.005)
                    continue
                if not chunk:
                    break
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    self.consume(line)
        except Exception as exc:
            self.error = exc

    def close(self):
        self.stop.set()
        self.thread.join()
        os.close(self.fd)
        if self.error:
            raise self.error
