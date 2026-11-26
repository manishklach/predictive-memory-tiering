# Running the synthetic module smoke test

The local build does not require a running EC2 instance. Use the designated, disposable Linux EC2 development host for module loading. Complete the v1 spec's T0 crash recovery rehearsal before treating the host as ready for module testing. The earlier Sydney `c7i-flex.large` boot test only verified OS and tooling.

On the prepared host, record the AMI, instance type, region and AZ, running kernel, module-signing state, and the kernel taint flags. Install headers matching `uname -r`, a C compiler, make, and Python 3. Mount debugfs at `/sys/kernel/debug` if it is available and has not already been mounted. Copy the repo to the host and run:

```sh
python3 harness/check_host.py --tier development --out /tmp/pmt-host.json
make test
make module
sudo sh harness/kernel_smoke.sh --dedicated-ec2
```

The smoke script verifies default-off behavior, reads synthetic records for a disposable target through both `read(2)` and the read-only ring mapping (`pmtd -m`), checks automatic disable when the reader exits, exercises the test failure control, and unloads the module. Save stdout, the kernel log, and taint flags to persistent storage. Formal M2 acceptance still requires the spec's 100-cycle unload test and leak and taint review on the pinned EC2 kernel.

The `/sys/class/misc/pmt_trace` controls are `target_pid` (host namespace), `enabled`, `interval_ms` (100–10,000), and `inject_failure`. Enabling requires a live target and an open reader. `/dev/pmt_trace` accepts one read-only reader at a time, and `/sys/kernel/debug/pmt/stats` reports emitted records, dropped records, and automatic disables for the current module lifetime.

Use `--tier nvme-test` before a performance campaign. That mode checks that all active swap devices are backed by AWS instance-store NVMe and that zswap is disabled. The checker is read-only and records blockers. A passing report covers these host checks. [AWS device identification](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-block-device-mapping.html)
