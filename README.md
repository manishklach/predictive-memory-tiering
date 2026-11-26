# Predictive memory tiering

An experimental Linux project to test whether proactive eviction and prefetch can reduce stalls at a smaller DRAM budget. The intended EC2 experiment uses local NVMe swap and compares the full system against stock Linux and detector-only tiering on identical hardware.

| Directory | Current implementation |
| --- | --- |
| `kernel/` | PID-scoped synthetic event module with sysfs controls, debugfs counters, and a bounded character-device queue |
| `daemon/` | C trace reader with optional CPU affinity and versioned record decoding |
| `harness/` | Offline reader checks and a scripted module smoke test for a dedicated EC2 host |
| `traces/` | Local capture and causal replay with eviction and prediction policies |
| `results/` | Replay manifest, input trace, and Sydney boot smoke log when available |
| `docs/` | Build instructions, milestone status, and the kernel smoke runbook |

On Linux with GCC, make, and Python:

```sh
make test
make module
```

The module emits synthetic records and defaults to disabled. The current reader interface uses `read(2)` and `poll(2)`. Page sampling, live eviction and prefetch, and the planned mmap/ioctl interface are future work. The replay measurements are modeled and the Sydney EC2 smoke test covered boot and kernel tooling.

- [Build and replay](docs/BUILD.md)
- [Dedicated EC2 kernel smoke test](docs/RUNBOOK.md)
