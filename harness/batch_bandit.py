"""Two-arm delayed epsilon-greedy page selection, scoped to one worker."""
from collections import deque
import random
import time

WINDOW_NS = 100_000_000
ARMS = ("noswap", "cost")


class BatchBandit:
    def __init__(self, seed):
        self.random = random.Random(seed)
        self.log = []
        self.pending = []
        self.history = {arm: deque(maxlen=4) for arm in ARMS}

    def mature(self, selector, now):
        ready = [b for b in self.pending if b["ready_ns"] <= now and
                 all("10" in selector.followup.pages[p] for p in b["pages"])]
        if not ready:
            return
        faults = selector.swap_trace.snapshot()
        for b in ready:
            freed = 0
            sleep = 0
            for p in b["pages"]:
                obs = selector.followup.pages[p]
                freed += int(obs["before"]["pte"] == "present" and obs["before"]["mincore_resident"]
                             and not obs["10"]["mincore_resident"])
                sleep += faults[p]["early_offcpu_ns"]
            b.update(feedback_ns=now, freed=freed, sleep_ns=sleep)
            self.history[b["arm"]].append((freed, sleep))
            self.pending.remove(b)

    def choose(self, selector, now):
        self.mature(selector, now)
        # Start with one batch from each arm. While labels mature, balance exposure.
        if not all(self.history.values()):
            counts = {a: sum(b["arm"] == a for b in self.log) for a in ARMS}
            return min(ARMS, key=counts.get), "warmup"
        if self.random.random() < 0.2:
            return self.random.choice(ARMS), "explore"
        def loss(arm):
            outcomes = self.history[arm]
            successes = sum(f for f, _ in outcomes)
            if not successes:
                return float("inf")
            return sum(ns for _, ns in outcomes) / successes
        return min(ARMS, key=loss), "exploit"

    def record(self, arm, reason, pages, decision_ns, query):
        if pages:
            batch = dict(arm=arm, reason=reason, pages=pages, decision_ns=decision_ns,
                         query=query, ready_ns=time.monotonic_ns() + WINDOW_NS)
            self.pending.append(batch)
            self.log.append(batch)

class TrialSchedule:
    """Use the latest DAMON decision for at most 100 ms, paced at 25 ms."""
    def __init__(self, selector, mode, seed, operations, constant_target=False, phase_boundary=None):
        self.selector = selector
        self.operations = operations
        self.phase_boundary = operations // 2 if phase_boundary is None else phase_boundary
        if not 0 < self.phase_boundary < operations:
            raise ValueError('phase boundary outside workload')
        self.consumed = 0
        self.region = None
        self.region_ns = self.last_attempt = 0
        self.mid_ns = self.mid_sleep = 0
        self.mid_advised = self.mid_confirmed = 0
        selector.target_pages = 24 if constant_target else 12
        selector.batch_size = 2
        if mode == "bandit":
            selector.bandit = BatchBandit(seed)

    def step(self, query, now, detector, start):
        s = self.selector
        s.query = query
        if query == self.phase_boundary + 1:
            self.mid_advised = len(s.advised)
            self.mid_confirmed = s.followup.confirmed if s.followup else 0
            self.mid_sleep = sum(p["slept_offcpu_ns"] for p in s.swap_trace.snapshot().values())
            self.mid_ns = now - start
            s.target_pages = 24
        if self.consumed < len(detector.decisions):
            self.consumed = len(detector.decisions)
            self.region_ns, self.region = detector.decisions[self.consumed - 1]
        if self.region and 0 <= now - self.region_ns <= WINDOW_NS and now - self.last_attempt >= 25_000_000:
            self.last_attempt = now
            s.apply(self.region)

    def report(self, times, elapsed, faults):
        total = sum(p["slept_offcpu_ns"] for p in faults.values())
        mid = self.phase_boundary
        def phase(samples, duration, sleep):
            count = len(samples)
            return dict(stall_ns=sleep, p99_us=sorted(samples)[(99*count+99)//100-1]/1000,
                        ops_per_s=count*1e9/duration)
        return dict(mid_advised=self.mid_advised, mid_confirmed=self.mid_confirmed, first=phase(times[:mid], self.mid_ns, self.mid_sleep),
                    second=phase(times[mid:], elapsed-self.mid_ns, total-self.mid_sleep),
                    decisions=self.selector.bandit.log if self.selector.bandit else [])
