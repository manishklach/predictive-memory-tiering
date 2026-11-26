"""Causal, discrete-event replay of anonymous 4 KiB pages."""

import argparse
from collections import Counter, OrderedDict, deque
import csv
from dataclasses import asdict, dataclass, field
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys
from time import process_time_ns


PAGE_BYTES = 4096
POLICIES = ("dram", "lru", "idle", "scored", "stride", "markov")


@dataclass(frozen=True)
class Access:
    timestamp_ns: int
    page: int
    write: bool = False


@dataclass(frozen=True)
class Settings:
    resident_ns: int = 100
    read_ns: int = 80_000
    write_ns: int = 100_000
    idle_ns: int = 10_000_000_000
    dwell_ns: int = 1_000_000_000
    lookahead: int = 100
    prefetch_cap: int = 1
    markov_contexts: int = 4096
    markov_order: int = 2


@dataclass
class History:
    last_ns: int
    last_index: int
    count: int = 0
    gap_mean: float = 0
    gap_m2: float = 0
    above_since: int | None = None
    minute: deque = field(default_factory=deque)
    ten_minutes: deque = field(default_factory=deque)

    def frequency(self, now):
        for window, seconds in ((self.minute, 60), (self.ten_minutes, 600)):
            while window and window[0] <= now - seconds * 10**9:
                window.popleft()
        return min(1, (len(self.minute) + len(self.ten_minutes) / 10) / 10)

    def touch(self, now, index):
        if self.count:
            gap = now - self.last_ns
            delta = gap - self.gap_mean
            self.gap_mean += delta / self.count
            self.gap_m2 += delta * (gap - self.gap_mean)
        self.count += 1
        self.last_ns = now
        self.last_index = index
        self.above_since = None
        self.minute.append(now)
        self.ten_minutes.append(now)
        self.frequency(now)


class Predictor:
    def __init__(self, settings):
        self.settings = settings
        self.context = ()
        self.transitions = OrderedDict()

    def observe(self, page, policy):
        order = self.settings.markov_order if policy == "markov" else 2
        context = self.context
        if policy == "markov" and len(context) == order:
            next_pages = self.transitions.setdefault(context, Counter())
            if page not in next_pages and len(next_pages) == 4:
                for candidate in list(next_pages):
                    next_pages[candidate] -= 1
                    if not next_pages[candidate]:
                        del next_pages[candidate]
            else:
                next_pages[page] += 1
            self.transitions.move_to_end(context)
            if len(self.transitions) > self.settings.markov_contexts:
                self.transitions.popitem(last=False)
        self.context = (*context, page)[-order:]

    def predict(self, policy):
        if policy == "stride" and len(self.context) == 2:
            previous, current = self.context
            stride = current - previous
            if stride:
                return (current + stride,)
        if policy == "markov":
            context = self.context
            predictions = []
            for _ in range(min(self.settings.prefetch_cap, self.settings.lookahead)):
                options = self.transitions.get(context)
                if not options:
                    break
                page = options.most_common(1)[0][0]
                if page in predictions:
                    break
                predictions.append(page)
                context = (*context, page)[-self.settings.markov_order:]
            return tuple(predictions)
        return ()


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else None


def replay(events, capacity, split, policy, cfg, weights=(1, .25, .5, 1)):
    resident = OrderedDict()
    backed = set()
    history = {}
    pending = {}
    expired = []
    ready = {}
    recent_evictions = {}
    predictor = Predictor(cfg)
    metrics = Counter()
    latencies, leads, lead_times = [], [], []
    completion = flash_free = 0
    scored_start = events[split].timestamp_ns

    def evict(page, index, now, scored):
        nonlocal flash_free
        dirty = resident.pop(page)
        if page in ready:
            metrics["wasted_prefetches"] += ready.pop(page)[2]
        if dirty or page not in backed:
            flash_free = max(now, flash_free) + cfg.write_ns
            metrics["flash_write_bytes"] += PAGE_BYTES * scored
            backed.add(page)
        recent_evictions[page] = index
        metrics["evictions"] += scored

    for index, event in enumerate(events):
        scored = index >= split
        page = event.page
        now = max(completion, event.timestamp_ns)
        while expired and expired[0][0] < index:
            deadline, candidate = heapq.heappop(expired)
            for requests in (pending, ready):
                request = requests.get(candidate)
                if request and request[1] == deadline:
                    metrics["wasted_prefetches"] += requests.pop(candidate)[2]
        for candidate, request in list(pending.items()):
            if request[0] <= now:
                resident[candidate] = False
                ready[candidate] = (index, *request[1:], request[0])
                del pending[candidate]

        request = pending.pop(page, None)
        if request:
            metrics["wasted_prefetches"] += request[2]
        if page in resident:
            completion = now + cfg.resident_ns
            if page in ready:
                finished, _, issued_scored, finished_ns = ready.pop(page)
                if issued_scored:
                    metrics["prefetch_hits"] += 1
                    leads.append(index - finished)
                    lead_times.append(now - finished_ns)
        else:
            if len(resident) + len(pending) == capacity:
                if pending:
                    cancelled = next(iter(pending))
                    metrics["wasted_prefetches"] += pending.pop(cancelled)[2]
                else:
                    evict(next(iter(resident)), index, now, scored)
            if page in backed:
                metrics["stalls"] += scored
                if index - recent_evictions[page] <= cfg.lookahead:
                    metrics["rapid_refaults"] += scored
                if request:
                    completion = request[0]
                else:
                    flash_free = max(now, flash_free) + cfg.read_ns
                    completion = flash_free
                    metrics["flash_read_bytes"] += PAGE_BYTES * scored
            else:
                completion = now + cfg.resident_ns

        resident[page] = resident.get(page, False) or event.write
        resident.move_to_end(page)
        history.setdefault(page, History(event.timestamp_ns, index)).touch(event.timestamp_ns, index)
        if scored:
            latencies.append(completion - event.timestamp_ns)

        predictions = ()
        if policy in ("stride", "markov"):
            started = process_time_ns()
            predictor.observe(page, policy)
            metrics["predictor_train_cpu_ns"] += (process_time_ns() - started) * scored
            started = process_time_ns()
            predictions = predictor.predict(policy)
            metrics["predictor_inference_cpu_ns"] += (process_time_ns() - started) * scored

        if policy not in ("dram", "lru") and len(resident) + len(pending) == capacity:
            idle_weight, recency_weight, frequency_weight, gap_weight = weights
            victim, highest_score = None, 1.0
            for candidate, dirty in resident.items():
                item = history[candidate]
                idle = event.timestamp_ns - item.last_ns
                score = idle / cfg.idle_ns
                if policy != "idle":
                    rank = min(1, (index - item.last_index) / capacity)
                    regular = item.count > 2 and item.gap_m2 / (item.count - 2) <= (.25 * item.gap_mean) ** 2
                    gap_penalty = regular and idle <= 1.25 * item.gap_mean
                    rate = item.frequency(event.timestamp_ns)
                    score = (idle_weight * score + recency_weight * rank
                             - frequency_weight * rate - gap_weight * gap_penalty - .25 * dirty)
                if score <= 1 or candidate in predictions or candidate == page:
                    item.above_since = None
                    continue
                if item.above_since is None:
                    item.above_since = event.timestamp_ns
                if score > highest_score and event.timestamp_ns - item.above_since >= cfg.dwell_ns:
                    victim, highest_score = candidate, score
            if victim is not None:
                evict(victim, index, completion, scored)

        for candidate in predictions[:cfg.prefetch_cap]:
            if len(resident) + len(pending) == capacity:
                break
            if candidate not in backed or candidate in resident or candidate in pending:
                continue
            flash_free = max(completion, flash_free) + cfg.read_ns
            deadline = index + cfg.lookahead
            pending[candidate] = (flash_free, deadline, scored)
            heapq.heappush(expired, (deadline, candidate))
            metrics["prefetch_requests"] += scored
            metrics["flash_read_bytes"] += PAGE_BYTES * scored

    metrics["wasted_prefetches"] += sum(request[2] for request in pending.values())
    metrics["wasted_prefetches"] += sum(request[2] for request in ready.values())
    count = len(latencies)
    requests = metrics["prefetch_requests"]
    result = {key: metrics[key] for key in (
        "stalls", "evictions", "rapid_refaults", "prefetch_requests", "prefetch_hits",
        "wasted_prefetches", "flash_read_bytes", "flash_write_bytes",
        "predictor_train_cpu_ns", "predictor_inference_cpu_ns",
    )}
    result.update(
        demand_accesses=count,
        stall_rate=metrics["stalls"] / count,
        p50_ns=percentile(latencies, .50),
        p99_ns=percentile(latencies, .99),
        prefetch_hit_rate=metrics["prefetch_hits"] / requests if requests else None,
        wasted_prefetch_rate=metrics["wasted_prefetches"] / requests if requests else None,
        prefetch_lead_p50_accesses=percentile(leads, .50),
        prefetch_lead_p50_ns=percentile(lead_times, .50),
        modeled_accesses_per_second=count * 1e9 / (completion - scored_start),
        flash_read_bytes_per_access=metrics["flash_read_bytes"] / count,
        flash_write_bytes_per_access=metrics["flash_write_bytes"] / count,
        markov_contexts=len(predictor.transitions),
        markov_transitions=sum(map(len, predictor.transitions.values())),
        markov_table_bytes_estimate=sys.getsizeof(predictor.transitions) + sum(
            sys.getsizeof(context) + sys.getsizeof(options)
            + sum(sys.getsizeof(value) for value in context)
            + sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in options.items())
            for context, options in predictor.transitions.items()
        ) if policy == "markov" else 0,
    )
    return result


def cold_fraction(events, split):
    last = {event.page: event.timestamp_ns for event in events[:split]}
    now = events[split - 1].timestamp_ns
    return {
        str(seconds): sum(now - stamp >= seconds * 10**9 for stamp in last.values()) / len(last)
        for seconds in (10, 60, 300, 900)
    }


def load_trace(path):
    with path.open(newline="", encoding="utf-8") as source:
        rows = csv.DictReader(source)
        if rows.fieldnames != ["timestamp_ns", "page", "op"]:
            raise ValueError("expected CSV header timestamp_ns,page,op")
        events = []
        for row in rows:
            timestamp, page = int(row["timestamp_ns"]), int(row["page"])
            if timestamp < 0 or page < 0 or row["op"] not in {"R", "W"}:
                raise ValueError(f"invalid access on line {rows.line_num}")
            if events and timestamp < events[-1].timestamp_ns:
                raise ValueError(f"timestamps go backwards on line {rows.line_num}")
            events.append(Access(timestamp, page, row["op"] == "W"))
        if len(events) < 10:
            raise ValueError("at least ten accesses are required")
        return events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--budget", type=int, choices=(25, 50, 75, 100), default=50)
    parser.add_argument("--idle-seconds", type=int, choices=(10, 60, 300, 900), default=10)
    parser.add_argument("--lookahead", type=int, choices=(100, 1000, 10000), default=100)
    parser.add_argument("--resident-ns", type=int, default=100)
    parser.add_argument("--read-ns", type=int, default=80_000)
    parser.add_argument("--write-ns", type=int, default=100_000)
    args = parser.parse_args()
    if min(args.resident_ns, args.read_ns, args.write_ns) <= 0:
        parser.error("service times must be positive")
    try:
        events = load_trace(args.trace)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    cfg = Settings(args.resident_ns, args.read_ns, args.write_ns,
                   idle_ns=args.idle_seconds * 10**9, lookahead=args.lookahead)
    split = len(events) // 5
    working_set = len({event.page for event in events})
    capacity = max(1, working_set * args.budget // 100)
    warmup = events[:split]
    weights_grid = [(1, .25, .5, 1), (1, .25, .5, 2), (1, .5, 1, 2)]

    def warmup_loss(weights):
        metrics = replay(warmup, capacity, 0, "scored", cfg, weights)
        return metrics["stalls"], metrics["p99_ns"], metrics["flash_write_bytes"]

    fit_started = process_time_ns()
    weights = min(weights_grid, key=warmup_loss)
    fit_ns = process_time_ns() - fit_started
    report = {
        "schema_version": 1,
        "mode": "local_replay",
        "timing_source": "user-configurable model assumptions",
        "trace": args.trace.name,
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "engine_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "warmup_accesses": split,
        "working_set_bytes": working_set * PAGE_BYTES,
        "target_budget_percent": args.budget,
        "budget_bytes": capacity * PAGE_BYTES,
        "effective_budget_percent": 100 * capacity / working_set,
        "settings": asdict(cfg),
        "weights": weights,
        "warmup_fit_cpu_ns": fit_ns,
        "warmup_end_cold_fraction_by_seconds": cold_fraction(events, split),
        "policies": {
            policy: replay(events, working_set if policy == "dram" else capacity, split,
                           policy, cfg, weights)
            for policy in POLICIES
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
