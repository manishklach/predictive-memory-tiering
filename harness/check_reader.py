"""Exercise the C reader with a binary ABI fixture and a truncated record."""

import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def main():
    reader = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as directory:
        trace = Path(directory) / "events.bin"
        record = struct.pack("=QQIHH", 2000, 9, 123, 1, 0)
        trace.write_bytes(record)
        command = [reader, "-d", str(trace)]
        good = subprocess.run(command, text=True, capture_output=True, check=True)
        assert json.loads(good.stdout) == {
            "timestamp_ns": 2000,
            "pid": 123,
            "page_id": 9,
            "access_type": "synthetic",
        }
        for invalid in (record[:-1], struct.pack("=QQIHH", 2000, 9, 123, 2, 0)):
            trace.write_bytes(invalid)
            bad = subprocess.run(command, text=True, capture_output=True)
            assert bad.returncode != 0 and not bad.stdout
        print("PMT trace reader passed")


if __name__ == "__main__":
    main()
