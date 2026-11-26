from random import Random
import unittest

from replay import Access, POLICIES, Settings, cold_fraction, replay


class ReplayTests(unittest.TestCase):
    def test_full_dram_and_reduced_lru_on_scan(self):
        events = [Access(index * 1000, index % 6) for index in range(60)]
        cfg = Settings(resident_ns=1, read_ns=20, write_ns=20)
        self.assertEqual(replay(events, 6, 12, "dram", cfg)["stalls"], 0)
        self.assertEqual(replay(events, 3, 12, "lru", cfg)["stalls"], 48)
        self.assertEqual(replay(events, 3, 12, "lru", cfg)["flash_write_bytes"], 0)

    def test_stride_reads_before_demand_and_late_reads_stall(self):
        events = [Access(index * 100, index % 6) for index in range(60)]
        fast = Settings(1, 20, 20, idle_ns=1, dwell_ns=0)
        slow = Settings(1, 200, 20, idle_ns=1, dwell_ns=0)
        stride = replay(events, 3, 12, "stride", fast)
        self.assertGreater(stride["prefetch_hits"], 0)
        self.assertLess(stride["stalls"], replay(events, 3, 12, "scored", fast)["stalls"])
        self.assertGreater(replay(events, 3, 12, "stride", slow)["stalls"], stride["stalls"])

    def test_markov_learns_non_strided_cycle(self):
        cycle = (0, 7, 3, 10, 1, 5)
        events = [Access(index * 1000, page) for index, page in enumerate(cycle * 20)]
        cfg = Settings(1, 20, 20, idle_ns=1, dwell_ns=0)
        self.assertLess(replay(events, 3, 24, "markov", cfg)["stalls"],
                        replay(events, 3, 24, "stride", cfg)["stalls"])

    def test_zipf_phase_and_slow_cycle_prefetch_accounting(self):
        random = Random(17)
        zipf = random.choices(range(12), weights=[1 / x for x in range(1, 13)], k=240)
        phase = [index % 4 + 4 * (index // 80) for index in range(240)]
        slow_cycle = [index % 4 if index % 30 else 8 + (index // 30) % 3
                      for index in range(240)]
        cfg = Settings(1, 70, 20, idle_ns=300, dwell_ns=100)
        for pages in (zipf, phase, slow_cycle):
            events = [Access(index * 100, page, index % 5 == 0)
                      for index, page in enumerate(pages)]
            for policy in POLICIES:
                with self.subTest(policy=policy):
                    result = replay(events, 5, 48, policy, cfg)
                    self.assertEqual(result["prefetch_requests"],
                                     result["prefetch_hits"] + result["wasted_prefetches"])
                    self.assertEqual(result["demand_accesses"], 192)
                    self.assertLessEqual(result["p50_ns"], result["p99_ns"])

    def test_cold_fraction_uses_warmup_only(self):
        events = [Access(0, 0), Access(15 * 10**9, 1), Access(20 * 10**9, 0)]
        self.assertEqual(cold_fraction(events, 2), {"10": .5, "60": 0, "300": 0, "900": 0})


if __name__ == "__main__":
    unittest.main()
