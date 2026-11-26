# Predictive memory tiering replay

## Linux module and reader

Run these commands from the repository root on Linux:

```sh
make test
make module
```

`make test` builds the C trace reader, controller, and Linux arena, and runs ABI, policy, fallback, and replay checks. `make module` builds with `W=1` against the running kernel's build tree by default. Set `KDIR=/path/to/build/tree` to compile against another prepared kernel. The C reader shares a fixed-size ABI header with the module and supports CPU affinity through `-c`. Kernel records are synthetic. [Cooperative runtime](LOCAL-RUNTIME.md) · [EC2 runbook](RUNBOOK.md)

### Isolated kernel test

With QEMU, cpio, a static BusyBox binary, and the boot image matching `KDIR`, run:

```sh
KDIR=/path/to/kernel/build sh harness/qemu_smoke.sh /path/to/bzImage /path/to/static/busybox
```

The guest exercises the read and mmap transports and performs 101 load/unload cycles. It boots a temporary RAM image with networking disabled and records the serial log under `/tmp` by default. This is a local correctness test. Formal M2 acceptance remains on the pinned EC2 kernel.

## Trace replay

The local replay harness uses Python 3.10+ and the standard library. Run these commands from the directory containing `pmt`.

```powershell
python -m unittest discover -s pmt/traces -v
python pmt/traces/capture_bfs.py pmt/results/bfs-trace.csv --vertices 2048 --rounds 3 --seed 17
python pmt/traces/replay.py pmt/results/bfs-trace.csv --budget 50 --out pmt/results/bfs-replay-50.json
```

Replay accepts CSV with header `timestamp_ns,page,op`, monotonically nondecreasing nonnegative timestamps, nonnegative logical page IDs, and operation `R` or `W`. The breadth-first search capture records accesses to its edge array and visited bitmap. It omits interpreter, queue, and OS memory accesses. Instrumentation affects recorded timing and each new capture produces a different timestamp sequence. Replay the checked-in trace to reproduce the modeled metrics. Predictor CPU time and fit CPU time are measured by the local Python process and can vary.

The first 20% of accesses warm the policies and fit the scored detector's weights using a fixed grid. Detector weights then stay fixed and Markov continues to learn from demand accesses as they occur. Capacity uses distinct logical pages across the trace as the all-resident reference, rounded down to whole pages with a minimum of one. The replay writes the effective budget and configuration to the JSON report.

The flash model serializes reads and writes on one queue and charges each operation a fixed service time. It retains backing for clean pages after swap-in, writes dirty pages when evicted, and reserves a resident slot while a prefetch is in flight. Cancellation releases the slot while already-issued traffic still occupies the modeled queue. The latency metric includes demand queueing behind previous accesses. The default service times are illustrative. Override them with `--resident-ns`, `--read-ns`, and `--write-ns`. The `lru` comparator is a replay policy. A measured stock reclaim comparison requires Linux and the dedicated EC2 setup.

Idle and scored policies fall back to demand LRU eviction when they cannot free room proactively. Replay exposes idle thresholds of 10, 60, 300, and 900 seconds and prediction horizons of 100, 1,000, and 10,000 accesses. It reports the cold fractions at the end of warmup. Markov stores bounded contexts and candidate counts. Its reported table size is an approximate Python object footprint and its CPU times characterize this replay implementation.

## Milestone status

M0 needs an authorized dedicated EC2 T0 environment to exercise the pinned kernel, kdump, serial console, and snapshot restore. For later kernel work, verify the exact Ubuntu kernel's available interfaces before choosing the module integration. Upstream Linux documents target-region `pageout` and `willneed` actions for DAMON's virtual-address operations, which provide a concrete path for an upstream baseline. [Linux 6.8 DAMON usage](https://docs.kernel.org/6.8/admin-guide/mm/damon/usage.html)

The local M1 prototype has synthetic checks and a replay report from a real algorithm. It still needs representative, long-running application traces and calibrated timing inputs before replay can inform the EC2 campaign. The synthetic M2 module and reader have local build checks. Live prediction and G1–G4 measurements remain open.
