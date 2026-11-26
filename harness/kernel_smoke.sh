#!/bin/sh
set -eu

if [ "${1:-}" != "--dedicated-ec2" ] || [ "$(id -u)" -ne 0 ]; then
	echo "Usage on the dedicated EC2 test host: sudo sh harness/kernel_smoke.sh --dedicated-ec2" >&2
	exit 2
fi

cd "$(dirname "$0")/.."
state=/sys/class/misc/pmt_trace
stats=/sys/kernel/debug/pmt/stats
work=$(mktemp -d)
loaded=0
target=
reader=
cleanup() {
	if [ "$loaded" -eq 1 ]; then
		echo 0 > "$state/enabled" || true
	fi
	for job in "$reader" "$target"; do
		if [ -n "$job" ]; then
			kill "$job" 2>/dev/null || true
			wait "$job" 2>/dev/null || true
		fi
	done
	if [ "$loaded" -eq 1 ]; then
		rmmod pmt || echo "Could not unload pmt during cleanup" >&2
	fi
	rm -f "$work/trace.jsonl" "$work/status"
	rmdir "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -d /sys/firmware/efi ]; then
	boot_state=$(mokutil --sb-state)
	case "$boot_state" in
		*"SecureBoot enabled"*)
			: "${PMT_MOK_KEY:?Set the path to the enrolled signing key}"
			: "${PMT_MOK_CERT:?Set the path to its DER certificate}"
			mokutil --test-key "$PMT_MOK_CERT"
			"/lib/modules/$(uname -r)/build/scripts/sign-file" sha256 "$PMT_MOK_KEY" "$PMT_MOK_CERT" kernel/pmt.ko
			;;
		*"SecureBoot disabled"*) ;;
		*) echo "Could not establish Secure Boot state: $boot_state" >&2; exit 1 ;;
	esac
fi
insmod kernel/pmt.ko
loaded=1
test "$(cat "$state/enabled")" = 0
test "$(cat "$state/target_pid")" = 0
test -r "$stats"
if (echo 1 > "$state/enabled") 2>/dev/null; then
	echo "PMT enabled without a target or reader" >&2
	exit 1
fi

sleep 60 &
target=$!
echo "$target" > "$state/target_pid"
echo 100 > "$state/interval_ms"

enable_when_reader_ready() {
	attempts=30
	until (echo 1 > "$state/enabled") 2>/dev/null; do
		attempts=$((attempts - 1))
		[ "$attempts" -gt 0 ] || return 1
		sleep 0.1
	done
}

./pmtd -n 4 > "$work/trace.jsonl" &
reader=$!
enable_when_reader_ready
wait "$reader"
reader=
test "$(cat "$state/enabled")" = 0
./pmtd -m -n 4 >> "$work/trace.jsonl" &
reader=$!
enable_when_reader_ready
wait "$reader"
reader=
test "$(cat "$state/enabled")" = 0
python3 - "$work/trace.jsonl" "$target" <<'PY'
import json
from pathlib import Path
import sys

events = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines()]
assert len(events) == 8
assert all(event["pid"] == int(sys.argv[2]) for event in events)
assert all(event["access_type"] == "synthetic" for event in events)
for batch in (events[:4], events[4:]):
    assert all(right["page_id"] == left["page_id"] + 1
               for left, right in zip(batch, batch[1:]))
PY

./pmtd -n 100 -t 10000 > /dev/null &
reader=$!
enable_when_reader_ready
echo 1 > "$state/inject_failure"
test "$(cat "$state/enabled")" = 0
cat "$stats" > "$work/status"
awk '$1 == "self_disables" && $2 == 3 { found = 1 } END { exit !found }' "$work/status"
cat "$work/status"
kill "$reader"
wait "$reader" 2>/dev/null || true
reader=
rmmod pmt
loaded=0
for cycle in $(seq 2 100); do
    insmod kernel/pmt.ko
    loaded=1
    test "$(cat "$state/enabled")" = 0
    rmmod pmt
    loaded=0
done
echo "PMT synthetic module smoke test passed (100 load/unload cycles)"
