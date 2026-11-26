import mmap
import unittest
from unittest.mock import MagicMock, patch

from harness.bounded_eviction import BoundedEviction, RECENT_NS


class RecentReadSelection(unittest.TestCase):
    def test_cross_page_read_is_protected_until_window_expires(self):
        page = mmap.PAGESIZE
        backing = MagicMock()
        backing.__len__.return_value = 3 * page
        selector = BoundedEviction(backing, page, None)
        selector.residency = lambda offset, pages: [1] * pages
        region = (page, 4 * page)
        with patch("harness.bounded_eviction.time.monotonic_ns", return_value=10**9) as clock:
            selector.on_read(page - 1, 2)
            clock.return_value += RECENT_NS
            selector.apply(region)
            self.assertEqual(set(selector.advised), {2})
            clock.return_value += 1
            selector.apply(region)
            self.assertEqual(set(selector.advised), {0, 1, 2})
            selector.on_read(0, page * 2)
            selector.finish(clock.return_value + RECENT_NS - 1)
        self.assertEqual(selector.stats["recent_skips"], 2)
        self.assertEqual(selector.stats["reused_within_100ms"], 2)
        self.assertEqual(selector.stats["full_window_pages"], 1)
        self.assertEqual(selector.stats["full_window_early_reuses"], 0)

    def test_longer_guard_keeps_reuse_metric_at_100ms(self):
        page = mmap.PAGESIZE
        backing = MagicMock()
        backing.__len__.return_value = 2 * page
        selector = BoundedEviction(backing, page, None, guard_ns=3 * RECENT_NS)
        selector.residency = lambda offset, pages: [1] * pages
        with patch("harness.bounded_eviction.time.monotonic_ns", return_value=10**9) as clock:
            selector.on_read(0, page)
            clock.return_value += 2 * RECENT_NS
            selector.apply((page, 3 * page))
            self.assertEqual(set(selector.advised), {1})
            clock.return_value += 2 * RECENT_NS
            selector.on_read(page, page)
            selector.finish(clock.return_value)
        self.assertEqual(selector.stats["full_window_pages"], 1)
        self.assertEqual(selector.stats["reused_within_100ms"], 0)

    def test_lru_ranks_eligible_pages_within_the_region(self):
        page = mmap.PAGESIZE
        backing = MagicMock()
        backing.__len__.return_value = 24 * page
        selector = BoundedEviction(backing, page, None, lru=True)
        selector.residency = lambda offset, pages: [int(i != 18) for i in range(offset // page, offset // page + pages)]
        with patch("harness.bounded_eviction.time.monotonic_ns", return_value=10**9) as clock:
            for i in reversed(range(1, 18)):
                selector.on_read(i * page, page)
                clock.return_value += 1
            clock.return_value += RECENT_NS + 1
            selector.on_read(20 * page, page)
            selector.apply((2 * page, 23 * page))
        self.assertEqual(list(selector.advised), [19, 21] + list(range(17, 3, -1)))
