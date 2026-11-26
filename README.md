# Predictive Memory Tiering

**Can a memory-bound Linux workload use half its usual DRAM and still keep tail latency under control?** PMT is an experimental Linux kernel and userspace project that studies this question using local NVMe swap on AWS EC2.

When memory is tight, stock Linux can swap an inactive page to NVMe and fetch it when the workload faults. A workload with predictable reuse may tolerate a smaller DRAM tier if software can identify pages that will stay cold and fetch swapped pages before their next access. The vision is to make the DRAM budget a tunable resource for workloads with that access pattern, while preserving the stock Linux fault path.

## How it works

1. A PID-scoped kernel module samples page activity and ranks eviction candidates by idle time, recency, frequency, reuse gaps, and write cost.
2. The module uses the Linux swap path to page out selected anonymous pages to local NVMe. Demand faults continue through the kernel's stock swap-in path.
3. A userspace process consumes the trace and predicts near-term reuse. The planned model ladder starts with stride and bounded Markov prediction within a one-core budget.
4. Prefetch uses available DRAM headroom. The experiment accounts for reads that arrive late, pages prefetched and never used, and the extra flash traffic from both.

The current module and reader implement the synthetic trace path. Real sampling, page-out, swap-in prefetch, and the final trace transport are planned. [System design](docs/DESIGN.md)

## What would count as a win?

The headline experiment gives each workload a DRAM budget equal to 50% of its measured all-DRAM working set. On the same EC2 hardware and local NVMe swap device, compare:

| Configuration | What it measures |
| --- | --- |
| All-DRAM | The workload's latency floor and working-set reference |
| Stock Linux, reduced budget | The reclaim and swap behavior PMT has to beat |
| DAMON/DAMOS where available | The upstream proactive page-out reference |
| PMT detector with prediction disabled | The effect of smarter eviction alone |
| PMT detector with stride prefetch | The value of a simple prediction rule |
| PMT detector with the selected predictor | The incremental value of prediction |

The headline result is a reduction in **demand stall rate and p99 workload latency** compared with both stock Linux and detector-only tiering. The campaign also measures throughput, flash write volume, prefetch waste and lead time, and predictor cost. The 50% budget is a test condition. [Experiment and milestones](docs/EXPERIMENT.md)

## Run the prototype

Use a Linux shell with GCC, make, and Python 3.10+. For the module build, install headers or provide a prepared build tree for the target kernel.

```sh
make test
make module
python3 traces/replay.py results/bfs-trace.csv --budget 50 --out /tmp/pmt-bfs-replay.json
```

`make test` builds the C reader and runs the ABI and replay checks. `make module` builds `kernel/pmt.ko` with `W=1`. The module starts disabled and the runtime smoke test belongs on a dedicated EC2 test host. [Build guide](docs/BUILD.md) · [EC2 runbook](docs/RUNBOOK.md)

## Current evidence

- The module built against local WSL Linux headers. Checkpatch, the sanitized reader checks, and five replay tests passed.
- A Sydney EC2 boot smoke test verified Ubuntu and matching kernel headers. It did not load the module or benchmark storage.
- In the checked-in breadth-first search replay, all reduced-budget policies produced the same **7.06% modeled demand stall rate**. The trace had no pages idle for the shortest configured threshold and issued zero prefetches. The target budget of 50% rounded down to four of nine pages. [Replay manifest](results/bfs-replay-50.json)

## Repository map

| Path | Contents |
| --- | --- |
| `kernel/`, `include/` | Synthetic kernel module and shared trace-record ABI |
| `daemon/` | Compiled C trace reader with optional CPU affinity |
| `traces/` | Instrumented graph traversal, replay engine, and policy tests |
| `harness/` | Binary reader fixtures and scripted module smoke test |
| `results/` | Checked-in replay input and manifest plus EC2 boot evidence |
| `docs/` | Design, experiment plan, build guide, and EC2 runbook |
