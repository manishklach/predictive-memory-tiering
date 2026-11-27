"""One-second read count saturated at four. Fit only same-run, same-page outcomes."""
from array import array
from collections import defaultdict


class ReadHistory:
    def __init__(self, pages):
        self.stamps = array("Q", [0]) * (pages * 4)
        self.next_slot = bytearray(pages)

    def record(self, page, now):
        slot = self.next_slot[page]
        self.stamps[page * 4 + slot] = now
        self.next_slot[page] = (slot + 1) % 4

    def count(self, page, now):
        cutoff = max(0, now - 1_000_000_000)
        return sum(t > cutoff for t in self.stamps[page * 4:page * 4 + 4])


def fit(runs):
    ages = defaultdict(lambda: [0, 0, 0])
    joint = defaultdict(lambda: [0, 0, 0])
    for row in runs:
        assert row["mode"] == "bounded-training-faulttrace"
        assert set(row["residency_followup"]) == set(row["swap_faults"])
        for page, obs in row["residency_followup"].items():
            if obs["before"]["pte"] != "present" or not obs["before"]["mincore_resident"] or "10" not in obs:
                continue
            ms = obs["last_read_age_ms"]
            age = str(-1 if ms is None else int(ms // 100).bit_length())
            key = f"{age}/{obs['recent_reads']}"
            for stats in (ages[age], joint[key]):
                stats[0] += 1
                stats[1] += int(not obs["10"]["mincore_resident"])
                stats[2] += row["swap_faults"][page]["slept_offcpu_ns"]
    if not any(sum(k.startswith(a + "/") for k in joint) > 1 for a in ages):
        raise ValueError("no within-age read-count variation")
    n, freed, sleep = map(sum, zip(*ages.values()))
    def table(groups):
        return {k: (v[2] + 32 * sleep / n) / (v[1] + 32 * freed / n) for k, v in groups.items()}
    age = table(ages)
    age["fallback"] = sleep / freed
    return dict(age=age, joint=table(joint), training_cells=dict(joint))
