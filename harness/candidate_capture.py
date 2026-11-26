"""Capture eligible sets and indexed VFS reads for matched selector replay."""
import json
import mmap
import struct
import time

from residency_followup import pagemap_state


READ = struct.Struct("<QIII")


class CandidateCapture:
    def __init__(self, directory, seed):
        self.epochs = (directory / f"{seed}.jsonl").open("w")
        self.reads = (directory / f"{seed}.bin").open("wb")
        self.pagemap = open("/proc/self/pagemap", "rb")
        self.query = 0

    def epoch(self, selector, pages, budget):
        rows = [[p, selector.last_read[p], pagemap_state(self.pagemap.fileno(), selector.address, p)]
                for p in pages]
        if rows:
            self.epochs.write(json.dumps(dict(ns=time.monotonic_ns(), query=self.query,
                                              budget=budget, candidates=rows)) + "\n")

    def read(self, offset, amount, now):
        self.reads.write(READ.pack(now, self.query, offset // mmap.PAGESIZE,
                                   (offset + amount - 1) // mmap.PAGESIZE))

    def close(self):
        self.epochs.close()
        self.reads.close()
        self.pagemap.close()
