# Predictive Memory Tiering

**Can a memory-bound Linux workload use half its usual DRAM and still keep tail latency under control?** PMT is an experimental Linux kernel and userspace project that studies this question using local NVMe swap on AWS EC2.

When memory is tight, stock Linux can swap an inactive page to NVMe and fetch it when the workload faults. A workload with predictable reuse may tolerate a smaller DRAM tier if software can identify pages that will stay cold and fetch swapped pages before their next access. The vision is to make the DRAM budget a tunable resource for workloads with that access pattern, while preserving the stock Linux fault path.

## How it works

The PID-scoped module emits synthetic sequence numbers through a bounded trace ring. A separate cooperative workload reports page accesses and residency to the C controller. The controller ranks eviction candidates and uses stride or Markov prediction. The workload applies the resulting advice to its own anonymous arena.

PMT can also observe region access in a running Linux process by PID using the DAMON kernel backend and a private tracefs instance. The live detector ranks those regions online with the C controller's cold score. Validation of eviction on local NVMe swap remains open. The replay detector and C controller currently use different scores, so replay cannot validate the controller's eviction decisions. [System design](docs/DESIGN.md) · [Cooperative runtime](docs/LOCAL-RUNTIME.md)

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

On Sydney EC2 the development guest recovered from a deliberate panic. The team verified the panic message in the reassembled dump and recovered the earlier smoke log from a snapshot-derived volume. [Crash and restore observations](results/sydney-recovery-crash.json)

## SQLite cold reuse and compiled-policy replay

The local SQLite workload alternates hot-key lookups with full scans of synthetic records. SQLite's VFS records main-database reads using monotonic completion timestamps. In the 305-second capture, 240 of 258 distinct database pages returned after a gap of at least 300 seconds. These are logical file reads under a deliberately scheduled workload. [Capture manifest](results/sqlite-long.json)

Offline replay now has an entry point that sends observations to the same compiled controller used by the cooperative arena. With a 129-page soft target, detector-only, stride, and Markov had 240, 128, and 219 modeled repeat misses respectively. All three exceeded the target on 1,240 observations and the simulator assumes immediate successful advice. [Compiled-controller results](results/sqlite-long-controller.json)

## Enforced local memory limit

The WSL anonymous arena charged 17,039,360 peak bytes to its calibration cgroup. Each comparison run used a fresh cgroup with memory.max set to 8,519,680 bytes. Median p99 touch latency over three runs was 716 microseconds for stock, 714 for detector-only, 992 for stride, and 799 for Markov. These measurements cover timed page touches. Throughput also includes instrumentation and controller IPC. [All runs](results/local-hard-limit.json)

A target-scoped smaps fixture identified known region touches and fed the observations to the compiled controller. A separate instrumented stride run observed 4,096 task read bytes across a WILLNEED syscall on a nonresident anonymous page and observed residency before returning to the workload. This verifies a cooperative return path on WSL swap. Module-originated live sampling and EC2 NVMe benchmarks remain open. [Region observations](results/local-regions.json) · [Prefetch evidence](results/local-cold-io.json)

On the Sydney development guest, the synthetic module completed 100 load/unload cycles on kernel 7.0.0-1018-aws with zero trace drops. Secure Boot was disabled. Taint flags included unsigned and out-of-tree module flags, and the running kernel configuration disabled DAMON. [Host audit](results/sydney-compat-host.json) · [Smoke evidence](results/sydney-compat-smoke.json)

## Local SQLite comparison at the 50% memory limit

Stock Linux handled this workload faster in all four pairs. Each run issued the same 3,000 single-client indexed lookups against 14,000 synthetic SQLite records, alternating between a 64-key hot set and the full table. The table reports medians across runs, with the full observed range in parentheses.

| Mode | Per-query p99, ms | Throughput, queries/s |
| --- | ---: | ---: |
| Stock Linux reclaim | 0.711 (0.577–0.829) | 9,839 (8,626–11,451) |
| Detector-only, prefetch off | 3.129 (2.953–7.824) | 628 (479–642) |

Both modes used the same read-only SQLite VFS backed by anonymous memory on WSL2. Each received a fresh cgroup with a 19,275,776-byte RAM limit, half the unconstrained stock peak rounded down to a page. Swap was active in every limited run. The detector shared the application's cgroup. For each logical page read it synchronously queried residency, sent the full residency map to pmtd, waited for advice, and requested page-out when advised. The timing includes those costs. The experiment measures this complete prototype path. It does not isolate scoring cost or measure EC2 NVMe performance.

Run order alternated by pair. Each query result was checked against its expected key and payload length. For reproduction on Linux with cgroup v2, swap, and Python APSW, build with `make pmtd`, then run `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-comparison.json`. [Harness](harness/sqlite_limit.py) · [Calibration and individual runs](results/local-sqlite-50.json)

### Profiling and reducing residency-check time

A full `mincore` scan of all 7,018 database pages on every logical page read was the largest measured detector cost. The SQLite adapter now refreshes the full map once per 16 observations, marks each touched page resident, and provisionally clears a victim after a successful page-out request. The controller still scores every observation. Linux can reclaim pages between scans and page-out is advisory, so the cached map can change victim selection. The full-scan mode remains available in the comparison.

All three modes ran four times on the same WSL2 host, with identical requests and a common 19,275,776-byte cgroup cap. Values are medians across runs. Stage times are cumulative wall-clock seconds during the 3,000 timed queries.

| Mode | p99 (ms) | Queries/s | Residency (s) | Controller round trip (s) | Page-out (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stock Linux | 0.700 | 10,265 | — | — | — |
| Full scan per observation | 3.497 | 616 | 2.980 | 1.203 | 0.073 |
| Refresh every 16 observations | 1.864 | 1,734 | 0.201 | 0.914 | 0.078 |

The change cut median detector p99 by 47% and raised throughput 2.82×. Stock Linux was faster in every repeat. The remaining largest measured stage is the synchronous controller round trip, which includes serialization, pipe transfers, scheduling, and detector scoring. Page-out timing covers the advice call and bookkeeping; kernel writeback may continue after the call returns. Other query work lies outside these three timers. Run the reproduction command above to repeat the three-way comparison. [Initial detector profile](results/sqlite-profile-before.json) · [Three-way comparison and per-run timers](results/sqlite-profile-50.json)

### Controller scoring and pipe communication

The pipe-backed detector spent 721 ms of its 838 ms controller round trip outside the C decision function over 3,000 queries. The SQLite adapter can now call the same scorer in-process with its existing residency buffer, removing a bitmap copy and a synchronous request/reply per observation. Both detectors refresh residency every 16 observations and issue page-out advice with prefetch disabled.

| Mode | Query p99 (ms) | Queries/s | Controller total (ms) | Scoring (ms) | Communication and wrapper (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stock Linux | 0.669 | 10,532 | — | — | — |
| Detector via pipes | 1.523 | 1,890 | 838 | 117 | 721 |
| Detector in-process | 1.162 | 3,722 | 141 | 104 | 36 |

Each value is the median across four runs on WSL2 at the same 19,275,776-byte cgroup limit. The controller columns accumulate elapsed time across a run. The C timer surrounds the decision function. Subtracting that time from the adapter's round-trip timer captures packing, transport, scheduling, and unpacking together. For the in-process path this remainder includes the Python-to-C call. Running the scorer in SQLite's process also eliminates the separate daemon's memory footprint under the cap.

The in-process path reduced median p99 by 24% and nearly doubled throughput. Stock Linux still led on both metrics. Every run returned the expected query results, used swap, and finished with zero OOM kills. [Per-run measurements](results/sqlite-roundtrip-50.json) · [Initial pipe profile with identical detector binaries in both arms](results/sqlite-roundtrip-pilot.json)

To reproduce on Linux with the SQLite benchmark prerequisites, build both profiled interfaces and pass the pipe executable by absolute path:

```sh
make pmtd pmtd-profile libpmt-profile.so
sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-roundtrip.json --profile-baseline "$PWD/pmtd-profile"
```

The recorded pipe baseline was built at commit `5d490ca`. The raw report includes hashes for both measured binaries.

### SQLite with observation, scoring, and page-out enabled in stages

Stock Linux had the lowest median p99 and highest median throughput. The largest throughput drop came when the adapter began observing residency: it refreshed the 7,018-page map every 16 page observations and marked each touched page resident. The scoring-only mode also ran the in-process C detector on every observation and counted its eviction advice. The final mode issued a page-out request for each advised victim. Linux reclaim remained active in all four modes.

| Mode | Median query p99 (ms) | Median throughput (queries/s) |
| --- | ---: | ---: |
| Stock Linux | 0.690 | 10,503 |
| Observation only | 0.868 | 6,364 |
| Observation and scoring | 1.006 | 4,278 |
| Observation, scoring, and page-out | 1.124 | 3,946 |

Each mode ran the same 3,000 queries four times on WSL2 and occupied each run position once. Each run had a fresh cgroup capped at 19,410,944 bytes, half the newly calibrated stock peak. All 16 runs returned the expected rows, used swap, and recorded zero OOM kills. Scoring-only issued zero page-out requests. The full detector issued 7,160–7,172 requests per run; the count measures advice submitted to Linux, not pages confirmed evicted.

Enabling page-out reduced median throughput by a further 7.8% and increased median p99 by 11.7% relative to scoring only. The full detector was slower in three of the four rounds on both metrics. These differences include the effect of subsequent memory accesses and reclaim. [Individual runs, counters, and calibration](results/sqlite-stages-50.json)

To reproduce, run `make pmtd libpmt-profile.so` and then `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-stages.json --stages`.

### Residency sampling intervals on SQLite

Refreshing the residency map every 256 logical page observations brought observation-only p99 within 0.003 ms of stock Linux in this local run. With scoring and page-out enabled, the same change improved throughput by 9.5% over the previous interval of 16, while median p99 rose by 3.3%.

| Mode | Observations per refresh | Scans per run | Median query p99 (ms) | Median queries/s |
| --- | ---: | ---: | ---: | ---: |
| Stock Linux | — | 0 | 0.707 | 10,659 |
| Observation only | 16 | 454 | 0.875 | 6,369 |
| Observation only | 64 | 114 | 0.884 | 7,445 |
| Observation only | 256 | 29 | 0.709 | 9,620 |
| Detector with page-out | 16 | 454 | 1.212 | 3,842 |
| Detector with page-out | 64 | 114 | 1.152 | 4,138 |
| Detector with page-out | 256 | 29 | 1.251 | 4,207 |

The interval controls full-map scans. Every logical read still updates its touched page, and the full detector still scores every observation. Increasing the interval leaves more time for Linux reclaim to change residency between scans. Median cumulative residency-check time in the full detector fell from 188 ms at 16 observations to 53 ms at 64 and 19 ms at 256. The lowest observed full-detector p99 was at 64, but individual run ranges overlap.

Each configuration ran once per round for four rounds in seeded randomized order, using the same 3,000 queries and a fresh cgroup capped at 19,406,848 bytes (50% of the calibrated stock peak rounded down to a page). All 28 runs passed query checks, used swap and recorded zero OOM kills. [Raw runs, counters, and timings](results/sqlite-sampling-50.json)

Reproduce with `make pmtd libpmt-profile.so` followed by `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-sampling.json --sampling-sweep`.

### Scoring cadence with residency sampled every 256 observations

Scoring every 64 logical page observations had the best median p99 and throughput among the four detector configurations. Every detector run updated touch history on all 7,258 observations and refreshed its residency map 29 times. The scoring interval controls how often the in-process detector ranks eviction candidates and can request page-out. Prefetch was disabled.

| Mode | Scoring calls per run | Page-out requests per run (range) | Median query p99 (ms) | Median queries/s |
| --- | ---: | ---: | ---: | ---: |
| Stock Linux | 0 | 0 | 0.848 | 8,980 |
| Score every observation | 7,258 | 5,301–6,543 | 1.122 | 4,589 |
| Score every 4 observations | 1,815 | 1,797–1,804 | 0.833 | 6,299 |
| Score every 16 observations | 454 | 449–451 | 0.942 | 6,596 |
| Score every 64 observations | 114 | 111–112 | 0.773 | 8,065 |

At interval 64, throughput increased 76% and p99 fell 31% relative to scoring every observation. Compared with stock, throughput was 10% lower and median p99 was 9% lower. Interval 64 had lower p99 than stock in three of four rounds, with overlapping run ranges. The measured change includes both fewer scoring passes and fewer page-out requests.

The WSL2 benchmark ran four seeded, randomized rounds at a common 19,406,848-byte RAM limit, half the calibrated stock peak rounded down to a page. Each run checked the same 3,000 query results, used swap, and recorded zero OOM kills. [Raw runs and counters](results/sqlite-scoring-50.json)

Reproduce with `make pmtd libpmt-profile.so` followed by `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-scoring.json --scoring-sweep`.

### Scoring every 64, 128, or 256 observations across 12 query seeds

Stock Linux led on both median metrics. Scoring every 256 observations gave the best detector result, reaching 90.9% of stock throughput with 8.3% higher p99. The earlier four-repeat p99 advantage at interval 64 did not repeat: interval 64 beat stock p99 in one of these 12 matched rounds.

| Mode | Median query p99 (ms) | Median queries/s | Rounds with lower p99 than stock | Rounds with higher throughput than stock |
| --- | ---: | ---: | ---: | ---: |
| Stock Linux | 0.715 | 9,653 | — | — |
| Score every 64 | 0.970 | 6,701 | 1/12 | 0/12 |
| Score every 128 | 0.821 | 8,120 | 2/12 | 1/12 |
| Score every 256 | 0.774 | 8,775 | 4/12 | 1/12 |

Each WSL2 round used one query seed shared across all four modes (seeds 12345–12356), with 3,000 queries per run. Each mode occupied each run position three times. All 48 runs used fresh cgroups capped at 19,406,848 bytes, half the calibrated stock peak rounded down to a page. The detector refreshed residency every 256 observations, maintained touch history on every observation, and could issue page-out advice only on a scoring pass. All runs passed query checks, used swap, and recorded zero OOM kills. [Per-run timings, seeds, and scoring counters](results/sqlite-scoring-extended-50.json)

Reproduce with `make pmtd libpmt-profile.so` followed by `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-scoring-extended.json --extended-scoring`.

### Formal Sydney T0 launch attempt

EC2 listed the designated `c7i.2xlarge` in three Sydney availability zones. The permission dry run succeeded, but the actual launch returned `InvalidParameterCombination` because the instance type is ineligible on the active Free account plan. The planned 256/256 rehearsal therefore produced no guest measurements. The earlier Sydney recovery evidence applies to the smaller compatibility host. [Launch record](results/sydney-t0-launch.json)

### Sydney formal T0 rehearsal on c7i.2xlarge

On November 26, 2026, the Sydney T0 host ran the 12-seed SQLite comparison on EBS-backed swap. At 256/256 the detector delivered 99.5% of stock median throughput and 8.2% lower median p99. It beat stock p99 in eight of twelve paired rounds and throughput in five. The measurements cover cooperative SQLite on one host with a small working set. T1 still needs live sampling, a DAMON-capable kernel, and the NVMe workload campaign.

| Mode (residency interval 256 for all detectors) | Median query p99 (ms) | Median queries/s |
| --- | ---: | ---: |
| Stock Linux | 1.719 | 4,200 |
| Score every 64 | 1.585 | 3,819 |
| Score every 128 | 1.557 | 4,170 |
| Score every 256 | 1.577 | 4,178 |

All 48 runs passed query checks at a common 19,529,728-byte cgroup limit (half the calibrated stock peak rounded down to a page), used swap, and recorded zero OOM kills. The backing device was the 8 GiB gp3 root volume; swap was a dedicated 512 MiB file. The fio baseline used a separate 256 MiB file. The 15-second direct 4 KiB random-read test at queue depth one measured 1,411 IOPS and 1.63 ms p99 completion latency. The short 1 MiB sequential write at queue depth eight measured 173 MiB/s. [SQLite runs](results/sydney-t0/sqlite.json) · [fio random reads](results/sydney-t0/fio-read.json) · [fio sequential writes](results/sydney-t0/fio-write.json)

The guest ran Ubuntu kernel 7.0.0-1018-aws with matching image and header packages held. Tests and module compilation passed, followed by 100 synthetic load/unload cycles with zero drops. Secure Boot was disabled. The unsigned out-of-tree module set the kernel taint value to 12288. The initial host audit recorded DAMON disabled. The recovery drill subsequently enabled kdump, verified the serial login prompt, triggered a deliberate panic, and extracted its panic message from the reassembled dump. The same EC2 instance then booted from a separate volume restored from the pre-panic snapshot and reproduced the benchmark file's SHA-256. The operator verified the private S3 archive by downloading and comparing it before stopping the guest. [Host audit](results/sydney-t0/host.json) · [Module smoke log](results/sydney-t0/smoke.log) · [Recovery evidence](results/sydney-t0/recovery.json)

### Ubuntu generic kernel preflight for DAMON

On November 26, 2026, the Ubuntu 24.04 amd64 package indexes selected the following kernels. Both packaged configurations contain `# CONFIG_DAMON is not set`. Neither candidate can provide the PID-scoped DAMON sysfs baseline, so this check required no EC2 boot or fio rerun.

| Header metapackage | Resolved generic build | SHA-256 of packaged .config |
| --- | --- | --- |
| linux-headers-generic | 6.8.0-151.151 | 2a95703373dff24b38861577b0cd79869e2fd7470ed532dca4ec34b37a7701b4 |
| linux-headers-generic-hwe-24.04 | 7.0.0-44.44~24.04.1 | d302f2fbed84b78593043047d7b736aa54aa4f53eeca1463b22e45bf7927cf88 |

To repeat the check, refresh Noble's package indexes, resolve each metapackage with `apt-cache depends`, download its versioned amd64 header package with `apt-get download`, and extract it with `dpkg-deb -x`. Inspect the configuration under `usr/src/linux-headers-<release>-generic/.config`, including commented-out symbols. A future candidate with DAMON, VADDR and SYSFS enabled still needs a boot test, a PID-scoped DAMOS page-out exercise and fresh same-kernel baselines.

### Sydney T0 with a DAMON-enabled Ubuntu AWS kernel

The custom `7.0.14-pmt-damon` kernel booted on both Sydney test hosts using AMI `ami-048c1a9eb63a67eed`. It was built in WSL2 from Ubuntu AWS source `7.0.0-1018.19~24.04.1`, with a two-line fix for an undeclared region-size parameter. The config delta includes changes to debugging, BTF-dependent features and trusted keys. Both hosts used Secure Boot disabled and retained the original AWS kernel as their normal GRUB fallback. [Build procedure](docs/BUILD.md#building-the-damon-capable-aws-test-kernel) · [Config delta](results/sydney-damon/config.diff) · [Package hashes](results/sydney-damon/packages.json)

The PID-scoped DAMOS probe applied page-out to 135,168 bytes on each host. The target process showed 76 KiB in swap on `c7i-flex.large` and 80 KiB on `c7i.2xlarge`, then read back the contents of its 32 MiB mapping successfully. Scheme counters measure bytes processed by DAMOS; process swap measures pages actually swapped at observation time. An initial probe configured an empty age range. Setting the upper bound explicitly made the scheme eligible to run. Both hosts passed the 100-cycle PMT synthetic module smoke test with zero trace drops. [Compatibility probe](results/sydney-damon/compat/damos.json) · [Formal probe](results/sydney-damon/formal/damos.json) · [Host configuration](results/sydney-damon/formal/host.json)

The formal host repeated the same EBS fio jobs. Direct queue-depth-one 4 KiB random reads measured 1,288 IOPS with 1.38 ms p99 completion latency. The short 256 MiB sequential write measured 244 MiB/s. These are single file-based measurements; the experiment did not benchmark ENA network throughput. [fio read](results/sydney-damon/formal/fio-read.json) · [fio write](results/sydney-damon/formal/fio-write.json)

| SQLite mode on the custom kernel | Median query p99 (ms) | Median queries/s |
| --- | ---: | ---: |
| Stock reclaim | 1.475 | 4,554 |
| PMT score every 64 | 1.555 | 4,161 |
| PMT score every 128 | 1.589 | 4,092 |
| PMT score every 256 | 1.647 | 4,017 |

DAMOS was off during these SQLite timings. All 48 runs passed query checks, used swap, and recorded zero OOM kills at the recalibrated 19,398,656-byte memory cap. PMT at 256/256 delivered 88.2% of stock throughput with 11.7% higher median p99. It beat stock p99 in four of twelve matched rounds and throughput in one. The earlier AWS-kernel median latency advantage did not repeat. The functional DAMOS probe establishes that this kernel can run a PID-scoped scheme; a timed SQLite DAMOS comparison and an instance-store NVMe campaign remain separate experiments. [Raw runs](results/sydney-damon/formal/sqlite.json)

The recovery drill booted the formal host from a disposable snapshot clone, checked the SQLite results against their pre-snapshot SHA-256, and booted the custom kernel on that clone. A deliberate panic produced a 289,984,155-byte dump. Reassembly recovered the custom kernel release and sysrq panic message. The guest returned to the original AWS fallback kernel with kdump rearmed. [Recovery manifest](results/sydney-damon/formal/recovery.json)

### Timed DAMOS baseline on the Sydney c7i.2xlarge

Stock Linux had the highest median throughput. PMT 256/256 had the lowest query p99 in every matched round. DAMOS fell between them on both median metrics. All three ran on the same custom kernel (`7.0.14-pmt-damon`) and gp3-backed swap.

| Mode | Median query p99 (ms) | Median queries/s | Rounds with lower p99 than stock | Rounds with higher throughput than stock |
| --- | ---: | ---: | ---: | ---: |
| Stock Linux | 1.576 | 4,415 | — | — |
| DAMOS PID-scoped page-out | 1.514 | 4,313 | 7/12 | 5/12 |
| PMT 256/256 | 1.321 | 3,579 | 12/12 | 1/12 |

DAMOS delivered 97.7% of stock throughput with 3.9% lower median p99. Compared with PMT, DAMOS had 20.5% higher throughput and 14.6% higher p99. The PMT latency ranking changed from the preceding custom-kernel experiment. These results describe the 36 runs in this comparison.

Each mode occupied each run position four times. Each round used the same 3,000 queries in all three modes, with seeds 12345–12356 across rounds. Each run had a fresh cgroup capped at 19,529,728 bytes, half the 39,059,456-byte stock calibration peak. All queries passed result checks, all runs used swap, and all recorded zero OOM kills.

DAMOS monitored the SQLite worker's anonymous database mapping with virtual-address sampling every 5 ms and aggregation every 100 ms. The page-out scheme selected regions with zero sampled accesses and age of at least two aggregation intervals. Scheme setup, counter collection, and teardown were outside query timing. At the end of the timed loop the kernel reported attempted actions in all 12 DAMOS runs and nonzero applied bytes in nine. Median cumulative applied bytes were 7,372,800 per run; these counters can include repeated actions on a region. A run with zero applied bytes still contributes to the comparison. [Calibration, per-run timings and counters](results/sydney-damon/formal/sqlite-timed-damos.json)

To reproduce on the dedicated host with the custom kernel, EBS swap active, and an unused DAMON admin interface, run `make pmtd libpmt-profile.so`, then `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-timed-damos.json --damos`.

### Live PID-scoped observation on Sydney T0

The PMT userspace sampler attaches to one PID using the kernel's DAMON virtual-address backend. It samples every 5 ms and records per-region access counts and age every 100 ms through a private tracefs instance filtered to its monitoring thread. It creates zero DAMOS schemes. The sampler can attach to a running program without application access callbacks. Its output is a statistical estimate at region granularity. The PMT kernel module's synthetic trace ring is a separate interface. [Sampler](daemon/live_sample.py)

On the Sydney `c7i.2xlarge`, a separate process alternated which of two 32 MiB anonymous ranges it read. Across four 1.5-second phases, the byte-weighted mean sampled hits per aggregation were 18.95–18.99 on the hot side and 0–0.053 on the cold side. The signal followed every reversal. This check supplied the range boundaries, separated the ranges by a page and disabled THP on the fixture mapping. An initial test with adjacent 4 MiB ranges failed to resolve the contrast; the passing test establishes discrimination for the reported fixture. Separate checks verified cleanup on target exit and reader SIGTERM. [Region measurements and shutdown checks](results/sydney-damon/formal/live-hot-cold.json)

| SQLite mode | Median query p99 (ms) | Median queries/s |
| --- | ---: | ---: |
| Stock Linux | 1.351 | 3,786 |
| Live PID observation | 1.218 | 4,196 |

The two arms alternated order across 12 paired seeds (12345–12356), each running the same 3,000 queries at a 19,529,728-byte memory cap. SQLite observation used automatic PID-wide region discovery, with 77–116 captured region records per run. All 24 runs passed result checks, used swap, and recorded zero OOM kills. Both arms issued zero PMT scores, residency scans and page-out requests. Kernel sampling and trace recording ran during the queries; sampler setup, trace readout and shutdown were outside query timing.

Observation had lower p99 in 8/12 rounds and higher throughput in 9/12. The measured medians show no slowdown in this workload. They do not establish that observing accelerates SQLite or quantify the observer's CPU use. [Calibration, per-seed timings and counters](results/sydney-damon/formal/sqlite-live-observe.json)

On the custom T0 kernel, use `sudo python3 daemon/live_sample.py PID --seconds 5` to collect a region trace. Reproduce the checks with `sudo python3 harness/check_live_sample.py` and `sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-live.json --live`, with swap active and the DAMON admin interface idle.

### Live cold-region scoring on Sydney T0

On the Sydney c7i.2xlarge running 7.0.14-pmt-damon, PMT scored complete 100 ms DAMON aggregates for one PID in a background thread. A sampled access updates region history. Idle regions become eligible after the cold-score and dwell checks. The detector can abstain. PMT issued no eviction advice or page-out requests.

A process alternated reads between two known 32 MiB ranges over four 1.8 s phases. The detector selected the cold range on all 48 decisions after the first 400 ms of each phase. Within those four transition windows it chose cold five times and abstained seven times. The fixture supplied both address ranges. SQLite used automatic region discovery. [Phase counts](results/sydney-damon/formal/live-detector-flips.json)

| SQLite at 50% memory | Median per-run p99 | Median throughput |
| --- | ---: | ---: |
| Stock Linux | 1.651 ms | 4,521 queries/s |
| Live sampling and PMT scoring | 1.363 ms | 4,080 queries/s |

Across 12 matched seeds with alternating order and 3,000 queries per run, live scoring had 17.4% lower median p99 and 9.7% lower median throughput. It had lower p99 in 10 pairs and higher throughput in five. All 24 runs used swap at the 19,529,728-byte limit, matched the paired query checksum and recorded zero OOM kills. During the timed queries PMT scored 71 epochs and selected a candidate in 43. SQLite timings measure the query loop with monitoring active and exclude attachment and teardown. The short runs establish live operation on EBS-backed T0. Eviction benefit and NVMe performance remain unmeasured. [Per-seed results](results/sydney-damon/formal/sqlite-live-detector.json)

To reproduce on the pinned T0 kernel with swap active and DAMON idle:

```sh
make libpmt-profile.so pmtd
sudo python3 harness/check_live_sample.py --score
sudo /usr/bin/python3 harness/sqlite_limit.py /tmp/sqlite-live-detector.json --live-score
```

### Profiling the SQLite throughput gap on Sydney T0

Each of three passes used the same 12 seeds, 3,000 queries per run, and all six orders of stock, sampling only and live scoring twice. All three passes used the first stock calibration's 50% cgroup limit. The second pass reduced the tracefs buffer from 256 to 32 KiB per CPU (2 MiB to 256 KiB requested across eight vCPUs). The third pass also increased the scoring reader idle wait from 5 to 50 ms. The 5 ms DAMON sampling and 100 ms aggregation intervals stayed fixed. The harness checks for trace overruns on teardown.

| Buffer per CPU | SQLite arm | Median p99 (ms) | Median queries/s | Worker CPU (ms) | Major faults | Peak swap (MiB) |
|---|---|---:|---:|---:|---:|---:|
| Stock (before) | Stock Linux | 1.574 | 4,586 | 82.8 | 779 | 31.75 |
| 256 KiB (before) | Sampling only | 1.570 | 3,991 | 81.8 | 841 | 33.60 |
| 256 KiB (before) | Live scoring | 1.354 | 4,038 | 106.1 | 880.5 | 32.64 |
| Stock (after) | Stock Linux | 1.726 | 4,254 | 85.2 | 802 | 31.79 |
| 32 KiB (after) | Sampling only | 1.477 | 4,198 | 86.7 | 832.5 | 33.50 |
| 32 KiB (after) | Live scoring | 1.438 | 3,997 | 107.1 | 880 | 33.37 |
| Stock (batched) | Stock Linux | 1.584 | 4,498 | 86.6 | 797.5 | 32.47 |
| 32 KiB (batched) | Sampling only | 1.551 | 4,254 | 87.3 | 840.5 | 31.64 |
| 32 KiB (batched) | Live scoring | 1.577 | 3,912 | 99.0 | 882 | 33.18 |

CPU and fault counts cover the query loop in the SQLite worker, including its scorer thread. Kernel DAMON thread CPU is outside this counter. Peak swap covers the worker's whole cgroup lifetime. The query timer excludes attach and teardown. Reproduce the first pass at commit 082cbac with `sudo /usr/bin/python3 harness/sqlite_limit.py before.json --live-profile`, then run the same command at fcabbf3 with `after.json --live-profile --paired-calibration before.json`. [Before](results/sydney-damon/formal/live-profile-before.json) · [After](results/sydney-damon/formal/live-profile-after.json) · [Batched reader](results/sydney-damon/formal/live-profile-batched.json) · [Batched cold-region check](results/sydney-damon/formal/live-profile-flip-batched.json) · [Cold-region regression check](results/sydney-damon/formal/live-profile-flip.json)

The retained configuration uses the 32 KiB buffer and original 5 ms reader wait. Sampling alone reproduced the initial loss. With smaller buffers, sampling throughput rose from 3,991 to 4,198 queries/s, while scoring throughput moved from 4,038 to 3,997. Stock also varied between passes. The median paired scoring throughput difference moved from -9.1% to -6.8%, with individual post-change pairs ranging from -17.1% to +8.4%. These trials establish a smaller buffer footprint and a remaining scoring gap. They do not isolate buffer allocation as the cause of the original loss.

The batched-reader trial at cebd6b4 cut median voluntary context switches from 1,720 to 1,108 and worker CPU from 107 to 99 ms. Its scoring throughput fell to 3,912 queries/s, so the reader change was reverted. With the retained settings, all 48 settled fixture decisions picked the cold range and the SQLite detector scored 75 epochs. Across all 108 timed runs, paired query checksums matched, swap was active, and PMT issued zero page-out requests. No OOM kills were recorded.
