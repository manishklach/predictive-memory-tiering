# Predictive Memory Tiering

**Can a memory-bound Linux workload use half its usual DRAM and still keep tail latency under control?** PMT is an experimental Linux kernel and userspace project that studies this question using local NVMe swap on AWS EC2.

When memory is tight, stock Linux can swap an inactive page to NVMe and fetch it when the workload faults. A workload with predictable reuse may tolerate a smaller DRAM tier if software can identify pages that will stay cold and fetch swapped pages before their next access. The vision is to make the DRAM budget a tunable resource for workloads with that access pattern, while preserving the stock Linux fault path.

## How it works

The PID-scoped module emits synthetic sequence numbers through a bounded trace ring. A separate cooperative workload reports page accesses and residency to the C controller. The controller ranks eviction candidates and uses stride or Markov prediction. The workload applies the resulting advice to its own anonymous arena.

The intended EC2 system will sample real target processes and tier their pages through local NVMe swap. Connecting that kernel path to the controller is still open. The replay detector and C controller currently use different scores, so replay cannot validate the controller's eviction decisions. [System design](docs/DESIGN.md) · [Cooperative runtime](docs/LOCAL-RUNTIME.md)

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
python3 harness/run_local.py --out /tmp/pmt-local.json
python3 traces/replay.py results/bfs-trace.csv --budget 50 --out /tmp/pmt-bfs-replay.json
```

`make test` builds the C reader, controller, and arena and runs the ABI, policy, and replay checks. `make module` builds `kernel/pmt.ko` with `W=1`. The module starts disabled and the module smoke test belongs on a dedicated EC2 test host. [Build guide](docs/BUILD.md) Â· [EC2 runbook](docs/RUNBOOK.md)

## Current evidence

The next EC2 milestone is the development-host recovery rehearsal. The Sydney smoke test established that the host boots and has matching kernel headers. The dedicated NVMe campaign still depends on live kernel page sampling, kernel-driven prefetch, and validation against stock reclaim on the pinned EC2 kernel. [EC2 runbook](docs/RUNBOOK.md)

- The module built against local WSL Linux headers. Checkpatch, the sanitized reader checks, and five replay tests passed.
- An isolated QEMU guest exercised both kernel trace readers and completed 101 module load/unload cycles. The cooperative runtime passed detector, predictor, data integrity, and controller-disconnect tests.
- The five-configuration Linux arena matrix exercised page-out and observed pages become nonresident. That run issued zero prefetch requests. Deterministic controller fixtures exercised both stride and Markov prefetch decisions. The runner also preserved the completed stock run and failed workload diagnostics during an injected cgroup error. [Reading a runtime manifest](docs/LOCAL-RUNTIME.md#reading-a-matrix-manifest)
- A Sydney EC2 boot smoke test verified Ubuntu and matching kernel headers. It did not load the module or benchmark storage.
- In the checked-in breadth-first search replay, all reduced-budget policies produced the same **7.06% modeled demand stall rate**. The trace had no pages idle for the shortest configured threshold and issued zero prefetches. The target budget of 50% rounded down to four of nine pages. [Replay manifest](results/bfs-replay-50.json)

## Repository map

| Path | Contents |
| --- | --- |
| `kernel/`, `include/` | Synthetic kernel module and shared trace-record ABI |
| `daemon/` | Compiled C trace reader and cooperative controller with stride and Markov prediction |
| `traces/` | Instrumented graph traversal, replay engine, and policy tests |
| `harness/` | Cooperative Linux arena and matrix runner, controller fixtures, and module smoke test |
| `results/` | Checked-in replay input and manifest plus EC2 boot evidence |
| `docs/` | Design, experiment plan, build guide, and EC2 runbook |

See the [first prerelease notes](docs/RELEASE-v0.1.0-alpha.1.md) for the verified scope and outstanding EC2 milestones.

## License

This project is licensed under the [GNU General Public License, version 2 only](LICENSE). The [userspace API header](include/pmt_uapi.h) carries the Linux syscall exception as indicated in its SPDX declaration.

The smaller Sydney recovery rehearsal started on November 26. [Startup evidence](results/sydney-rehearsal-start.json)

The Sydney guest passed both EC2 health checks and exposed its login prompt over the authenticated serial console. [Recovery access evidence](results/sydney-recovery-access.json)
