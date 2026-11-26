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
    length = 24 * 1024 * 1024
    buf = mmap.mmap(-1, length, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    address = ctypes.addressof(ctypes.c_char.from_buffer(buf))
    for i in range(0, length, size):
        buf[i] = 17
    buf.madvise(getattr(mmap, "MADV_PAGEOUT", 21))
    pressure = bytearray(18 * 1024 * 1024)
    candidates = list(range(128, length // size, 32))
    bits = (ctypes.c_ubyte * (length // size))()
    libc = ctypes.CDLL(None, use_errno=True)
    for _ in range(100):
        if libc.mincore(ctypes.c_void_p(address), length, bits):
            raise OSError(ctypes.get_errno(), "mincore")
        cold = [i for i in candidates if not bits[i] & 1]
        if len(cold) >= 17:
            break
        time.sleep(.01)
    else:
        raise RuntimeError("swapped controls still cached")
    unregistered, *tagged = cold[:17]
    assert buf[0] == 17
    trace = SwapTrace()
    trace.mark(address, 0)
    with open("/proc/self/pagemap", "rb") as pm:
        for i in tagged:
            assert pagemap_state(pm.fileno(), address, i) == "swapped"
            trace.mark(address + i * size, i)
        assert buf[0] == 17
        assert buf[unregistered * size] == 17  # Unregistered negative control.
        for i in tagged:
            assert buf[i * size] == 17
    result = trace.finish()
    assert set(result) == {0, *tagged} and result[0]["calls"] == 0
    assert all(result[i]["calls"] >= 1 for i in tagged)
    assert all(r["handler_ns"] >= r["slept_offcpu_ns"] for r in result.values())
    assert sum(r["slept_offcpu_ns"] for r in result.values()) > 0, result
    print(json.dumps(result))


if __name__ == "__main__":
    validate()

