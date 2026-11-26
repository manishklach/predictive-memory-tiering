"""Task-scoped binary DAMON aggregate stream with a single perf mmap ring."""
import ctypes
import mmap
import os
from pathlib import Path
import re
import struct
import sys

EVENT = Path("/sys/kernel/tracing/events/damon/damon_aggregated")


class AggregateRing:
    def __init__(self, tid, capacity):
        if capacity < mmap.PAGESIZE or capacity % mmap.PAGESIZE or capacity & (capacity - 1):
            raise ValueError("perf ring requires a power-of-two number of bytes in whole pages")
        fmt = (EVENT / "format").read_text()
        self.fields = []
        for name in ("target_id", "nr_regions", "start", "end", "nr_accesses"):
            match = re.search(r"\b" + name + r";\s*offset:(\d+);\s*size:(\d+);", fmt)
            if not match:
                raise ValueError(f"missing DAMON event field {name}")
            self.fields.append(tuple(map(int, match.groups())))
        lib = ctypes.CDLL(str(Path(__file__).resolve().parents[1] / "libpmt-profile.so"), use_errno=True)
        self.copy = lib.pmt_trace_copy
        self.copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self.buffer = ctypes.create_string_buffer(capacity)
        self.fd = lib.pmt_trace_open(int((EVENT / "id").read_text()), tid)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "perf_event_open damon_aggregated")
        try:
            self.mapping = mmap.mmap(self.fd, mmap.PAGESIZE + capacity, flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE)
        except BaseException:
            os.close(self.fd)
            raise
        self.ptr = ctypes.addressof(ctypes.c_char.from_buffer(self.mapping))

    def read(self):
        size = self.copy(self.ptr, self.buffer, len(self.buffer))
        if size < 0:
            raise OSError(-size, "DAMON perf ring overflow")
        return self.buffer[:size]

    def decode(self, data, detector):
        offset = 0
        while offset < len(data):
            kind, _, size = struct.unpack_from("=IHH", data, offset)
            if size < 20 or offset + size > len(data) or kind != 9:  # PERF_RECORD_SAMPLE
                raise RuntimeError(f"incomplete, lost or unexpected perf record {kind}")
            stamp, raw_size = struct.unpack_from("=QI", data, offset + 8)
            if raw_size > size - 20:
                raise ValueError("truncated DAMON perf payload")
            raw = data[offset + 20:offset + 20 + raw_size]
            values = []
            for pos, width in self.fields:
                if pos + width > raw_size:
                    raise ValueError("DAMON perf field exceeds payload")
                values.append(int.from_bytes(raw[pos:pos + width], sys.byteorder))
            target, count, start, end, hits = values
            if target == 0:
                detector.aggregate(stamp, count, start, end, hits)
            offset += size

    def close(self):
        self.mapping.close()
        os.close(self.fd)
