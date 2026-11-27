"""Regression checks for the delayed-feedback experiment."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from batch_bandit import BatchBandit, TrialSchedule, WINDOW_NS


class BanditTest(unittest.TestCase):
    def test_waits_for_mature_sleep_and_completed_residency(self):
        observed = {1: {"before": {"pte": "present", "mincore_resident": True}}}
        trace = SimpleNamespace(snapshot=lambda: {1: {"early_offcpu_ns": 73, "slept_offcpu_ns": 999}})
        selector = SimpleNamespace(followup=SimpleNamespace(pages=observed), swap_trace=trace)
        bandit = BatchBandit(0)
        with patch("batch_bandit.time.monotonic_ns", return_value=10):
            bandit.record("cost", "warmup", [1], 5, 10)
        bandit.mature(selector, 10 + WINDOW_NS)
        self.assertFalse(bandit.history["cost"])
        observed[1]["10"] = {"mincore_resident": False}
        bandit.mature(selector, 9 + WINDOW_NS)
        self.assertFalse(bandit.history["cost"])
        bandit.mature(selector, 10 + WINDOW_NS)
        self.assertEqual(list(bandit.history["cost"]), [(1, 73)])
        bandit.mature(selector, 100 + WINDOW_NS)
        self.assertEqual(len(bandit.history["cost"]), 1)

    def test_zero_yield_arm_cannot_win_exploitation(self):
        bandit = BatchBandit(0)
        bandit.history["noswap"].append((0, 0))
        bandit.history["cost"].append((1, 1000000))
        self.assertEqual(bandit.choose(None, 100), ("cost", "exploit"))

    def test_identical_phase_budgets_and_stale_region_expiry(self):
        for mode in ("bandit", "cost", "noswap"):
            actions = []
            selector = SimpleNamespace(apply=actions.append, bandit=None, advised={}, followup=None,
                       swap_trace=SimpleNamespace(snapshot=lambda: {}))
            schedule = TrialSchedule(selector, mode, 13, 60000)
            t = 1_000_000_000
            detector = SimpleNamespace(decisions=[(t, (100,200))])
            schedule.step(1,t,detector,t)
            schedule.step(2,t+1,detector,t)
            self.assertEqual((selector.target_pages,selector.batch_size),(12,2))
            schedule.step(30001,t+WINDOW_NS+1,detector,t)
            self.assertEqual(selector.target_pages,24)
            self.assertEqual(actions,[(100,200)])
