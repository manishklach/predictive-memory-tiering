# Cooperative Linux runtime

The local runtime keeps the prediction and eviction policy in a compiled `pmtd` process. The workload owns a private anonymous arena and samples its residency with `mincore(2)`. After each access it sends the page ID, timestamp, write flag, and residency snapshot to the controller. The workload applies returned advice only to its own arena. This lets us test the policy, controller lifecycle, and Linux advice behavior without loading a module or using a GPU or an EC2 instance.

```sh
make
make test
./pmt-local markov 128 64 4 8 2 1000
python3 harness/run_local.py --out /tmp/pmt-local.json
```

The arena arguments are policy, arena pages, target pages, rounds, idle threshold in milliseconds, dwell in milliseconds, and microseconds between accesses. An optional final CPU number pins only the controller. The experiment uses 4 KiB pages and disables THP for this mapping. Page contents are checked on every access. The matrix runs an unconstrained stock control, stock at the configured target, detector-only, stride, and Markov.

## Advice and measurements

The controller tracks idle time, decays observed frequency between accesses, and penalizes evictions of dirty or regularly reused pages. Its fixed 4,096-entry table learns an order-two Markov transition online. A single reply can recommend at most one eviction and one prefetch. Eviction requires a cold observed page, completed dwell, and either a budget overage or a candidate prefetch with the budget exactly full. Prefetch requires an observed, currently nonresident target and confirmed headroom. If the controller exits, sends an invalid reply, or exceeds the response deadline, the arena continues with native behavior and exits with a failed run status.

The arena uses `MADV_PAGEOUT` and `MADV_WILLNEED`. An accepted advice call does not guarantee eviction or useful prefetch. The report records immediate residency after page-out and observed residency when a prefetched page is accessed. Residency alone does not attribute the page's return to prefetch or prove that a nonresident access reached physical storage. Touch p50/p99 measures the timed arena access, and elapsed time includes instrumentation and configured pacing. The manifest records swap configuration and binary hashes for context. [Linux man-pages](https://www.kernel.org/pub/linux/docs/man-pages/book/man-pages-6.8.pdf)

The target page count is a policy threshold. For a real cgroup limit, provide an already configured, delegated cgroup v2 directory with `PMT_WORKLOAD_CGROUP`. The arena joins it after spawning the controller, so only the workload enters this cgroup. The matrix records `memory.max` and `memory.swap.max` and leaves the unconstrained stock control outside that group. On a machine without a delegated cgroup, the two stock labels use the same native memory behavior.

## Reading a matrix manifest

Check the top-level `complete` field first. Each entry in `runs` includes a configuration label and child exit code. If a child fails, the runner stops the matrix, saves the earlier runs and available diagnostics, and exits with an error. It writes the manifest to a temporary file in the output directory and atomically replaces the destination.

Use `advice_errors` and `controller_disconnected` to diagnose a failed arena run. Compare `eviction_attempts` with `immediate_nonresident_after_pageout` to see how often a residency check observed reclamation immediately. Read `prefetch_advice_accepted` alongside `resident_on_prefetched_access`, `nonresident_on_prefetched_access`, and `unused_prefetch`. A run with zero accepted prefetches provides no measurement of prefetch effectiveness.

## Integration status

The arena is a cooperative test workload. Live access sampling through the kernel module, the prefetch request ioctl, asynchronous swap-in, flash I/O attribution, and the representative EC2 workload campaign are separate milestones. Use this harness to check decisions, failure handling, and data integrity before connecting the compiled policy to those interfaces.
