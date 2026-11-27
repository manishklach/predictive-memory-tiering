# Experiment plan

## Test matrix

Run a key-value workload with skewed reuse, an OLTP database workload with periodic scans or checkpoints, and an analytics workload that alternates scans and lookups. Measure each working set in an all-DRAM control, then set the cgroup DRAM budget at 100%, 75%, 50%, and 25% of that working set. Keep compared runs on the same hardware, kernel, and swap device.

For each workload, report the cold fraction before policy evaluation. Use a chronological 20% warm-up and 80% scored split by default. Keep kernel configuration, CPU placement, storage setup, and workload inputs consistent across baselines. Run the headline 50% comparison at least three times and report the spread per workload. The synthetic and instrumented local replay traces exercise policy behavior with modeled timings.

## Hardware and evidence

The Sydney Free Tier boot smoke used an EBS-only `c7i-flex.large`. Formal development targets `c7i.2xlarge` for kernel and recovery checks. The primary performance host in the spec is `i7i.4xlarge` with local instance-store NVMe swap. Larger or bare-metal hosts are for later validation. Verify instance-store identity before collecting results and capture a device baseline for each test host.

Each formal run needs a manifest containing the code revision, instance type and location, AMI and kernel, CPU assignments, swap device and baseline measurements, sampling and model parameters, workload and budget, costs, and counters. Record stall rate, p50 and p99 operation latency, throughput, flash reads and writes, prefetch hit and waste rates, lead time, and predictor resource use. Record trace drops and self-disables so they can be accounted for when determining eligibility. Preserve results before stopping an instance with ephemeral storage.

## Milestones and decisions

| Milestone | Evidence required |
| --- | --- |
| M0 · EC2 development environment | Pinned kernel, feature checks, kdump capture, serial-console access, and snapshot recovery rehearsal |
| M1 · Local replay | Known-structure tests and a reproducible application trace report |
| M2 · Synthetic module | Default-off runtime smoke, failure handling, and 100 load/unload cycles on the pinned EC2 kernel |
| M3 · Real eviction | PID-scoped page-out on local NVMe swap with reconciled counters and stock fallback |
| M4 · Detector-only campaign | **G1:** detector improves stall rate or p99 over stock reclaim on a majority of real workloads at 50% DRAM |
| M5 · Live prediction | **G2:** build the larger model only if Markov leaves measurable, structured headroom |
| M6 · Full-system campaign | **G3:** the full system improves both stall rate and p99 against stock Linux and detector-only tiering on a majority of real workloads at 50% DRAM across repeat runs. **G4:** report write volume per workload hour alongside the result |
| M7 · Bare metal | After G3 passes, repeat a predeclared subset of configurations on bare metal |

A failed gate is an experimental result. Keep the workload, thresholds, and metric definitions fixed for the campaign and publish which baseline held up and where. Run any revised protocol as a separately labeled campaign with fresh baselines.

### Page-level selector headroom

Sydney `i7i.2xlarge` is the NVMe pilot. Formal T1 remains on the roadmap on `i7i.4xlarge` with larger working sets.

A chronological replay of the 22,528-read SQLite scheduled-scan trace found zero observable headroom within the selected regions. Address order, LRU, a fitted reuse-risk table and an oracle with future knowledge each selected 0 pages reused within 100 ms out of 2,752 selections, and 48 within 64 VFS reads out of 2,800. Counts include repeated selection opportunities. This small trace cannot establish whether page-level prediction improves the Sydney workload.

The screen splits by event index, fits on the first half and scores the second. It excludes training labels that cross the split and incomplete outcome windows. Every 64 reads it groups previously seen pages by aligned 32-page file ranges and applies the 100 ms recent-read guard. Among groups with more than 16 eligible pages, it chooses the group whose newest eligible read is oldest. All selectors choose 16 pages from the same group. The risk table uses logarithmic idle-time and access-count buckets with Laplace smoothing. Regions are simulated, residency is unknown, and decisions do not evict pages. The trace records reads rather than query boundaries.

The next pilot capture needs eligible page IDs at each real decision, their pre-advice residency and read history, and timestamped subsequent reads with query IDs. Those records allow matched-volume oracle and held-out seed comparisons on actual candidate sets before a timed policy trial.

Reproduce with `python traces/page_headroom.py results/sqlite-long.csv results/sqlite-page-headroom.json`.

#### Reproducing the matched-byte comparison

On the pinned x86-64 pilot guest, provision the same exclusive NVMe swap, disable zswap, enable the memory cgroup controller and build `libpmt-profile.so`. Build and load the diagnostic module from `kernel/swapfault` and enable `kernel.task_delayacct` before starting new workers. Run the swap-fault validation twice. The SQLite entry point is:

```sh
python3 harness/sqlite_limit.py sqlite.json --requests 60000 --cost-trial \
  --paired-calibration results/sydney-damon/formal/bounded-pageout.json
```

For the Redis pilot, use Redis 7.0.15, its matching `redismodule.h` (SHA-256 `dcc23952d82cd8f06e4fe6ee32de0b04d437cfe03779963230b3f0dde2c731e0`), the Ubuntu Python 3.12 development headers and `redis-cli`. In the following commands `REDIS_INCLUDE` is the header directory and `REDIS_SERVER` is the absolute executable path.

```sh
cc -shared -fPIC -O2 -I"$REDIS_INCLUDE" $(python3-config --includes) \
  -DPMT_LIBPYTHON='"libpython3.12.so.1.0"' harness/redis_cost/redis_bridge.c \
  -o /var/tmp/pmt-redis-bridge.so $(python3-config --embed --ldflags)
python3 harness/redis_cost/run.py "$REDIS_SERVER" /var/tmp/pmt-redis-bridge.so redis.json
```

The Redis harness opens one disposable Unix-socket-only server per arm. It estimates the memory cap from half an unconstrained server's peak cgroup memory rounded down to a page. The timed loop executes native GETRANGE calls on a 32 MiB string, alternating uniformly random pages and a 64-page hot set. It checks every reply. The embedded benchmark driver and sampler share the server's cgroup; the reported latency is server-side and excludes network time. Redis uses the frozen SQLite training tables. Both drivers run as root on the isolated guest for DAMON, cgroups and the diagnostic device. Unload the module and restore delay accounting after the trial.

### Delayed two-arm bandit trial

The adaptive worker starts fresh for each seed. It chooses between filtered LRU and the same frozen cost-aware ranking used by the fixed comparator. Every worker filters swap entries, has a two-page batch limit, and uses the latest cold region for at most 100 ms with at least 25 ms between attempts. The cumulative target is 12 confirmed 4 KiB transitions during the first half of the run and 24 overall. This staging preserves decisions after the Redis workload changes. Unused first-half slots carry over.

A bandit batch becomes eligible for feedback after 100 ms and after its 10 ms residency observations have completed. The kernel clips tagged sleeping intervals at each page's 100 ms deadline. On each subsequent choice, the worker consumes matured batches. For each arm it estimates loss as the sum of clipped sleeping nanoseconds divided by confirmed reclaim successes across its four latest matured batches. Zero total successes receive infinite loss. It balances initial exposure until both arms have feedback, explores uniformly with probability 0.2, and otherwise chooses the arm with the smaller estimated loss. Decision records include page IDs, query index, timestamps, and available feedback. Whole-run evaluation uses total tagged sleep, including sleep beyond the training window.

The planned comparison uses seeds 32345–32356, 60,000 operations per seed, every arm order twice, and a half-memory cap per workload. Redis moves its 64-page hot set from offsets 0–63 to 4096–4159 after operation 30,000. Uniform random reads continue over the whole string. Compare all runs for target attainment, throughput and p99, and compare sleep on seeds meeting the same target in all arms. Report the two halves separately. The criterion for a win is lower paired stall than the better fixed policy over the whole run with comparable target attainment. The kernel probe must be rebuilt and both controls must pass before timing.

Use `--bandit-trial` in the SQLite matched-cost command above and `PMT_BANDIT_TRIAL=1` for the Redis driver. The summary command is `python3 harness/summarize_bandit.py trial.json.gz summary.json`.
