"""Check that offline feedback yields the arena protocol's expected decisions."""

from pathlib import Path
import sys
import unittest

from replay import Access
from replay_controller import run


@unittest.skipUnless(sys.platform.startswith("linux") and Path("pmtd").is_file(),
                     "build the Linux controller with make")
class ControllerParity(unittest.TestCase):
    def test_regular_reuse_and_dirty_penalty(self):
        no_page = 2**32 - 1
        for dirty, expected in [(False, 1), (True, no_page)]:
            audit = []
            events = [Access(t, 0) for t in (0, 10_000_000, 20_000_000)]
            events.extend([Access(20_000_000, 1, dirty), Access(31_500_000, 2)])
            run(events, Path("pmtd"), 4, 2, 10, 0, "none", audit)
            self.assertEqual(audit[-1][1], (expected, no_page))

    def test_dirty_and_dwell(self):
        no_page = 2**32 - 1
        audit = []
        events = [Access(0, 0, True), Access(20_000_000, 1), Access(26_000_000, 1)]
        run(events, Path("pmtd"), 4, 1, 10, 5, "none", audit)
        self.assertEqual([reply for _, reply in audit],
                         [(no_page, no_page), (no_page, no_page), (0, no_page)])
