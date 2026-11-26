# System design

## Memory and storage path

PMT targets named workload processes inside a memory cgroup. The cgroup supplies the DRAM budget and normal reclaim remains available throughout the run. The v1 storage tier is dedicated swap on EC2 instance-store NVMe. The intended eviction path reuses Linux page-out and swap accounting. Anonymous pages can receive swap backing, while clean file-backed pages can be discarded and must be counted separately. A demand access to a swapped page continues through the normal kernel fault path.

Sampling is periodic. The planned implementation evaluates page-table accessed bits and checks its observations against DAMON on the pinned kernel. Idle time leads the coldness score. Recency, recent frequency, regularity of reuse, and dirty-page write cost help distinguish pages that can remain on flash from pages likely to return soon. A dwell interval suppresses short-lived threshold crossings, and near-term predicted pages are excluded from proactive eviction. Detector weights are fitted on chronological warm-up data and frozen for scoring.

## Trace and control path

The prototype trace module uses a bounded FIFO and a versioned 24-byte record. It exposes a root-only reader device, sysfs controls, and debugfs counters. Recorded page IDs are synthetic sequence numbers. The module retains the target's kernel PID reference and disables emission on target exit, reader disconnect, or an injected test failure. The C reader consumes records and emits JSON Lines. It can pin itself to a CPU for tests.

The planned live interface uses a memory-mapped ring for trace export and an ioctl for bounded prefetch batches. It needs explicit accounting for drops, completed and late prefetches, wasted pages, bytes read and written, and self-disable events. Kernel support and module-accessible APIs must be verified against the selected Ubuntu EC2 kernel before the real page-out and prefetch paths are finalized.

## Prediction and backpressure

The replay engine implements stride and bounded Markov predictors. Live `pmtd` will train from observed accesses and use no future trace data. An EC2 campaign must pin the daemon to one core and record its CPU quota, training and inference time, and memory use. A larger model is justified only if Markov misses show structured and measurable headroom.

Each prefetch batch has a configured request cap and deadline. A request must fit within DRAM headroom and refer to a page on the tier. A completed page is useful if demand reaches it before eviction. A late completion still leaves a demand stall. When the daemon disappears or the module self-disables, proactive work should stop within a sampling interval. Existing swapped pages remain accessible through the stock path.
