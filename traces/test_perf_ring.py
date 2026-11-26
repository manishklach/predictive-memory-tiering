"""Check perf aggregation decoding independently of privileged kernel access."""
import struct
import unittest
from unittest.mock import Mock

from daemon.perf_ring import AggregateRing


class PerfAggregates(unittest.TestCase):
    def test_batch_records_and_reject_loss(self):
        stream = AggregateRing.__new__(AggregateRing)
        stream.fields = [(0, 4), (4, 4), (8, 8), (16, 8), (24, 4)]
        raw = struct.pack("=IIQQI", 0, 2, 4096, 8192, 3)
        sample = struct.pack("=IHHQI", 9, 0, 20 + len(raw), 123, len(raw)) + raw
        detector = Mock()
        stream.decode(sample * 2, detector)
        self.assertEqual(detector.aggregate.call_count, 2)
        detector.aggregate.assert_called_with(123, 2, 4096, 8192, 3)
        with self.assertRaises(RuntimeError):
            stream.decode(struct.pack("=IHHQQ", 2, 0, 24, 1, 5), detector)
        with self.assertRaises(RuntimeError):
            stream.decode(sample[:-1], detector)
