#!/bin/sh
set -eu

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
	echo "Usage: sh harness/qemu_smoke.sh BZIMAGE STATIC_BUSYBOX [SERIAL_LOG]" >&2
	exit 2
fi

image=$(realpath "$1")
busybox=$(realpath "$2")
log=$(realpath -m "${3:-/tmp/pmt-qemu-smoke.log}")
cd "$(dirname "$0")/.."
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

make module
mkdir -p "$work/root/bin" "$work/root/dev" "$work/root/proc" "$work/root/sys"
cp "$busybox" "$work/root/bin/busybox"
cp kernel/pmt.ko "$work/root/pmt.ko"
cp harness/qemu_guest_init.sh "$work/root/init"
chmod +x "$work/root/init"
cc -Iinclude -O2 -Wall -Wextra -Werror -static daemon/pmtd.c daemon/control.c -o "$work/root/pmtd"
(cd "$work/root" && find . -print0 | cpio --quiet --null -o -H newc) > "$work/initramfs"

if ! timeout 90 qemu-system-x86_64 -machine q35,accel=tcg -cpu max -smp 2 -m 384 \
	-nographic -no-reboot -nic none -kernel "$image" -initrd "$work/initramfs" \
	-append 'console=ttyS0 rdinit=/init panic=-1 loglevel=4' > "$log" 2>&1; then
	tail -n 80 "$log" >&2
	exit 1
fi
if ! grep -q '^PMT_QEMU_PASS' "$log" ||
	grep -Eiq 'WARNING: CPU:|BUG:|Kernel panic|Oops:' "$log"; then
	tail -n 80 "$log" >&2
	exit 1
fi
grep '^PMT_QEMU_' "$log"
printf 'Serial log: %s\n' "$log"
