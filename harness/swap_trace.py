"""Query-thread swap-fault probe and present/swapped validation fixture."""
import ctypes
import json
import mmap
import os
import struct
import time

from residency_followup import pagemap_state


class SwapTrace:
    def __init__(self):
        self.fd = os.open("/dev/pmt_swapfault", os.O_RDWR)
        self.pages = []

    def mark(self, addr, page):
        os.write(self.fd, struct.pack("@L", addr))
        self.pages.append(page)

    def finish(self):
        with os.fdopen(self.fd) as stream:
            header, *lines = stream.read().splitlines()
        if header != "missed 0" or len(lines) != len(self.pages):
            raise RuntimeError(f"incomplete swap trace: {header}, {len(lines)} rows")
        result = {}
        for line in lines:
            i, calls, handler_ns, delay_ns, offcpu_ns = map(int, line.split())
            result[self.pages[i]] = dict(calls=calls, handler_ns=handler_ns, swapin_delay_ns=delay_ns, slept_offcpu_ns=offcpu_ns)
        return result


def validate():
    size = mmap.PAGESIZE
    buf = mmap.mmap(-1, 3 * size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    address = ctypes.addressof(ctypes.c_char.from_buffer(buf))
    for i in range(3):
        buf[i * size] = 17 + i
    trace = SwapTrace()
    trace.mark(address, 0)
    trace.mark(address + size, 1)
    with open("/proc/self/pagemap", "rb") as pm:
        for i in (1, 2):
            buf.madvise(getattr(mmap, "MADV_PAGEOUT", 21), i * size, size)
            for _ in range(100):
                if pagemap_state(pm.fileno(), address, i) == "swapped":
                    break
                time.sleep(.01)
            else:
                raise RuntimeError("control failed to swap")
        time.sleep(.1)
        for i in (0, 1, 2):
            assert buf[i * size] == 17 + i
    result = trace.finish()
    assert set(result) == {0, 1} and result[0]["calls"] == 0 and result[1]["calls"] >= 1
    assert result[1]["handler_ns"] >= result[1]["slept_offcpu_ns"] > 0, result
    print(json.dumps(result))


if __name__ == "__main__":
    validate()

