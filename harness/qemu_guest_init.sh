#!/bin/busybox sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
set -eu
export PATH=/bin
/bin/busybox --install -s /bin

finish() {
	rc=$?
	if [ "$rc" -ne 0 ]; then
		echo "PMT_QEMU_FAIL exit=$rc"
	fi
	poweroff -f
}
trap finish EXIT

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t debugfs debugfs /sys/kernel/debug
state=/sys/class/misc/pmt_trace
stats=/sys/kernel/debug/pmt/stats
initial_taint=$(cat /proc/sys/kernel/tainted)
insmod /pmt.ko
test "$(cat "$state/enabled")" = 0
if (echo 1 > "$state/enabled") 2>/dev/null; then
	exit 1
fi
echo "$$" > "$state/target_pid"
echo 100 > "$state/interval_ms"

for method in read mmap; do
	if [ "$method" = mmap ]; then
		set -- -m
	else
		set --
	fi
	/pmtd "$@" -n 4 > /trace.jsonl &
	reader=$!
	sleep 1
	echo 1 > "$state/enabled"
	wait "$reader"
	test "$(wc -l < /trace.jsonl)" -eq 4
	test "$(grep -c "\"pid\":$$,\"page_id\"" /trace.jsonl)" -eq 4
	test "$(cat "$state/enabled")" = 0
	echo "PMT_QEMU_TRANSPORT $method"
done
grep -q '^self_disables 2$' "$stats"
grep -q '^trace_drops 0$' "$stats"
rmmod pmt

for cycle in $(seq 1 100); do
	insmod /pmt.ko
	rmmod pmt
done
final_taint=$(cat /proc/sys/kernel/tainted)
# Out-of-tree and unsigned module flags are expected in an unsigned test image.
test "$((final_taint & ~(initial_taint | 4096 | 8192)))" -eq 0
echo "PMT_QEMU_PASS unload_cycles=101 taint_before=$initial_taint taint_after=$final_taint"
