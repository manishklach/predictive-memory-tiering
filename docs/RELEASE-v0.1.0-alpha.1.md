## PMT v0.1.0-alpha.1

This first public prerelease packages the local Linux prototype and the pre-EC2 review fixes. It provides a reproducible starting point for the development-host recovery rehearsal.

### Runtime and tooling

- The PID-scoped kernel module starts disabled and exports synthetic records through a character device and a read-only mapped ring. It counts dropped events and automatic disables.
- The C controller runs detector-only, stride, and online Markov policies against a cooperative anonymous-memory workload. The workload applies Linux page-out and prefetch advice and checks data integrity after reclaim.
- The local matrix records binary hashes, policy settings, exit codes, and diagnostics. A failed workload preserves the completed runs in the manifest.
- The replay harness compares policies after a chronological warm-up. The Python replay and C runtime currently use different detector scores.

### Changes from the readiness review

The README now identifies the synthetic event source, and the build examples run from the repository root. A GitHub Actions workflow runs the userspace tests. The test target invokes checkpatch when the selected kernel build tree provides it.

The dedicated-host smoke script checks Secure Boot state, supports signing with an enrolled MOK, and performs 100 load/unload cycles. A separate operator-side helper archives staged results and verifies an S3 readback before stopping a tagged instance.

### Verification and known limits

The latest local review ran the reader, controller, arena, and five replay tests, built the module against local Linux headers, and reported zero checkpatch warnings. An earlier QEMU run exercised both trace transports and completed 101 module load/unload cycles. Secure Boot signing, archival on AWS, and the revised EC2 unload loop still await a dedicated-host rehearsal.

The checked-in BFS replay used four of nine logical pages at the nominal 50% budget. Every reduced-budget policy recorded 3,124 modeled stalls and zero prefetch requests. The trace is a correctness fixture. Predictive latency improvement on real workloads remains an open question.

Before the T1 performance campaign, the project needs representative traces, shared detector scoring, live kernel sampling and prefetch integration, measured storage baselines, and the T0 crash and recovery rehearsal.
