"""Exercise causal decisions, protocol validation, and the Linux arena."""

import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


NONE = 2**32 - 1


def message(ms, page, resident, wrote=0):
    bitmap = bytes(index in resident for index in range(4))
    return struct.pack("=QII", ms * 1_000_000, page, wrote) + bitmap


def decisions(daemon, events, model="none", budget=2, dwell=0):
    process = subprocess.run(
        [daemon, "--control", "4", str(budget), "10", str(dwell), model],
        input=b"".join(events), capture_output=True, check=True, timeout=4,
    )
    return list(struct.iter_unpack("=II", process.stdout))


def main():
    daemon, arena = (str(Path(arg).resolve()) for arg in sys.argv[1:])
    events = [message(0, 0, {0}, 1), message(20, 1, {0, 1}), message(26, 1, {0, 1})]
    assert decisions(daemon, events, budget=1, dwell=5) == [
        (NONE, NONE), (NONE, NONE), (0, NONE),
    ]
    stride = [message(0, 2, {2}), message(20, 0, {0}), message(30, 1, {1})]
    assert decisions(daemon, stride, "stride")[-1] == (NONE, 2)
    assert decisions(daemon, stride, "stride", budget=1)[-1] == (NONE, NONE)
    paired = [message(0, 2, {2}), message(1, 3, {3}),
              message(20, 0, {0, 3}), message(30, 1, {1, 3})]
    assert decisions(daemon, paired, "stride")[-1] == (3, 2)
    markov = [message(index, page, {page}) for index, page in enumerate([0, 3, 1, 0, 3])]
    assert decisions(daemon, markov, "markov") == [(NONE, NONE)] * 4 + [(NONE, 1)]
    assert decisions(daemon, markov, "stride")[-1] == (NONE, NONE)
    bad = subprocess.run(
        [daemon, "--control", "4", "1", "10", "0", "none"],
        input=message(0, 5, {0}), capture_output=True, timeout=4,
    )
    assert bad.returncode != 0 and not bad.stdout

    for policy in ("stock", "none", "stride", "markov"):
        result = subprocess.run(
            [arena, policy, "16", "8", "4", "1", "0", "1000"],
            capture_output=True, text=True, check=True, timeout=8,
        )
        report = json.loads(result.stdout)
        assert report["policy"] == policy and report["repeat_accesses"] == 48
        assert not report["controller_disconnected"]
        if policy == "stock":
            assert report["eviction_attempts"] == report["prefetch_advice_accepted"] == 0
        else:
            assert report["eviction_attempts"] > 0
        assert report["prefetch_advice_accepted"] == (
            report["resident_on_prefetched_access"]
            + report["nonresident_on_prefetched_access"] + report["unused_prefetch"]
        )
    with tempfile.TemporaryDirectory() as directory:
        stub = Path(directory) / "pmtd"
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o700)
        result = subprocess.run(
            [arena, "markov", "16", "8", "4", "1", "0", "0"], cwd=directory,
            capture_output=True, text=True, timeout=8,
        )
        report = json.loads(result.stdout)
        assert result.returncode and report["controller_disconnected"]
        assert report["accesses"] == 64 and report["eviction_attempts"] == 0
    print("PMT controller and Linux arena passed")


if __name__ == "__main__":
    main()
