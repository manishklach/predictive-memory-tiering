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
