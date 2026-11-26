# Running the synthetic module smoke test

The local QEMU test exercised 101 load/unload cycles. On EC2, first rehearse recovery on the designated development host and retain the crash dump and restore evidence. Then build against that host's running kernel and repeat the module checks below. The subsequent NVMe campaign requires a working live sampler and prefetch path before stock-versus-PMT comparisons can begin.

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

## Secure Boot and archival

On EFI hosts the smoke script requires mokutil. When Secure Boot is enabled, supply PMT_MOK_KEY and PMT_MOK_CERT in the root execution environment. The certificate must already be enrolled and in DER format. The script verifies enrollment and signs the module with the running kernel's sign-file tool. The functional cycle and 99 subsequent cycles make up the 100-cycle smoke test. Save the kernel logs for leak and taint review.

After collecting all completed run manifests and logs into an operator-side directory, use the following command with a private S3 destination and the intended instance ID.

~~~sh
bash harness/archive_and_stop.sh "$PROFILE" "$REGION" "$INSTANCE_ID" "$RESULTS_DIR" "$S3_PREFIX"
~~~

The helper checks the Project=pmt instance tag, archives the supplied directory, uploads it under its SHA-256 digest, downloads it, compares the bytes, and then requests and waits for the stop. Archive verification covers the files already staged. The operator must verify campaign completeness before invoking the command.

For the Sydney rehearsal, record Free Tier balance before startup, during compute use, and after stopping. Record snapshot and restore completion independently. Restore the regional serial console setting after the rehearsal. [Starting observations](../results/sydney-rehearsal-start.json)
