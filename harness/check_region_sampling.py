"""Feed Linux referenced-bit observations from four isolated VMAs to pmtd."""

import ctypes
import json
import mmap
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path


def workload(channel):
    libc = ctypes.CDLL(None, use_errno=True)
    size = mmap.PAGESIZE
    with mmap.mmap(-1, 9 * size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS) as arena:
        base = ctypes.addressof(ctypes.c_char.from_buffer(arena))
        for i in range(9):
            if i % 2:
                arena[i * size] = i
            elif libc.mprotect(ctypes.c_void_p(base + i * size), ctypes.c_size_t(size), 0):
                raise OSError(ctypes.get_errno(), "mprotect")
        channel.sendall(struct.pack("=Q", base))
        while mask := channel.recv(1):
            for i in range(4):
                if mask[0] & (1 << i):
                    assert arena[(2 * i + 1) * size] == 2 * i + 1
            bits = (ctypes.c_ubyte * 9)()
            if libc.mincore(ctypes.c_void_p(base), ctypes.c_size_t(9 * size), bits):
                raise OSError(ctypes.get_errno(), "mincore")
            channel.sendall(bytes(bits[2*i+1] & 1 for i in range(4)))


def main():
    parent, child = socket.socketpair()
    pid = os.fork()
    if pid == 0:
        parent.close()
        try:
            workload(child)
        finally:
            child.close()
        os._exit(0)
    child.close()
    proc = Path(f"/proc/{pid}")
    samples = []
    try:
        parent.settimeout(5)
        base, = struct.unpack("=Q", parent.recv(8, socket.MSG_WAITALL))
        addresses = {base + (2*i+1)*mmap.PAGESIZE: i for i in range(4)}
        with subprocess.Popen(["./pmtd", "--control", "4", "2", "1", "0", "none"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as daemon:
            try:
                for mask in (15, 1, 1, 8):
                    (proc / "clear_refs").write_text("1")
                    time.sleep(.01)
                    parent.sendall(bytes([mask]))
                    resident = parent.recv(4, socket.MSG_WAITALL)
                    observed = []
                    index = None
                    for line in (proc / "smaps").read_text().splitlines():
                        header = re.match(r"^([0-9a-f]+)-([0-9a-f]+) ", line)
                        if header:
                            index = addresses.get(int(header[1], 16))
                        elif index is not None and line.startswith("Referenced:"):
                            if int(line.split()[1]):
                                observed.append(index)
                    decisions = []
                    for index in observed:
                        daemon.stdin.write(struct.pack("=QII", time.monotonic_ns(), index, 0)
                                           + resident)
                        daemon.stdin.flush()
                        evict, _ = struct.unpack("=II", daemon.stdout.read(8))
                        decisions.append(None if evict == 2**32 - 1 else evict)
                    assert set(observed) == {i for i in range(4) if mask & (1 << i)}, observed
                    samples.append(dict(touch_mask=mask, observed=observed, evictions=decisions))
            finally:
                daemon.stdin.close()
            assert not daemon.wait()
    finally:
        parent.close()
        _, status = os.waitpid(pid, 0)
        if status:
            raise RuntimeError("target exited abnormally")
    print(json.dumps({"source": "target smaps Referenced after clear_refs",
                      "kernel": os.uname().release, "samples": samples}, indent=2))


if __name__ == "__main__":
    main()
