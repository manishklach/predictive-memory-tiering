"""Linux x86-64 TASKSTATS and reclaim counters for the SQLite query thread."""
import os
from pathlib import Path
import socket
import struct


def attributes(data):
    offset = 0
    while offset + 4 <= len(data):
        length, kind = struct.unpack_from("=HH", data, offset)
        if length < 4 or offset + length > len(data):
            raise ValueError("invalid netlink attribute")
        yield kind & 0x3fff, data[offset + 4:offset + length]
        offset += (length + 3) & ~3


class FaultProfile:
    def __init__(self):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16)
        self.sock.settimeout(2)
        self.sock.bind((0, 0))
        reply = self.request(16, 3, 2, b"TASKSTATS\0")
        self.family = struct.unpack("=H", reply[1])[0]
        path = Path("/proc/self/cgroup").read_text().split("0::", 1)[1].strip()
        self.group = Path("/sys/fs/cgroup") / path.lstrip("/")

    def request(self, family, command, kind, value):
        attr = struct.pack("=HH", len(value) + 4, kind) + value
        payload = struct.pack("=BBH", command, 1, 0) + attr + bytes(-len(attr) % 4)
        self.sock.sendto(struct.pack("=IHHII", 16 + len(payload), family, 1, 1, 0) + payload, (0, 0))
        reply = self.sock.recv(65536)
        length, kind = struct.unpack_from("=IH", reply)
        if kind == 2:
            err = -struct.unpack_from("=i", reply, 16)[0]
            raise OSError(err, os.strerror(err))
        return dict(attributes(reply[20:length]))

    def snapshot(self):
        stats = dict(line.split() for line in (self.group / "memory.stat").read_text().splitlines())
        result = {key: int(stats[key]) for key in ("pgscan_direct", "pgsteal_direct")}
        for key in ("zswpin", "zswpout", "zswpwb"):
            if key in stats:
                result[key] = int(stats[key])
        vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
        for key in ("pswpin", "pswpout", "swap_ra", "swap_ra_hit"):
            if key in vm:
                result["host_" + key] = int(vm[key])
        reply = self.request(self.family, 1, 1, struct.pack("=I", os.getpid()))
        raw = dict(attributes(reply[4]))[3]
        # Stable x86-64 linux/taskstats.h offsets, including v6 reclaim accounting.
        if struct.unpack_from("=H", raw)[0] < 6:
            raise ValueError("kernel TASKSTATS version lacks reclaim delays")
        for key, offset in (("blkio_ns", 40), ("swapin_ns", 56), ("reclaim_ns", 320)):
            result[key] = struct.unpack_from("=Q", raw, offset)[0]
        return result
