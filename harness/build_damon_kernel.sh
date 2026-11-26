#!/usr/bin/env bash
# Build from an already-extracted Ubuntu AWS source tree and its matching amd64 config.
set -euo pipefail
src=$(realpath "${1:?Ubuntu AWS kernel source directory}")
base=$(realpath "${2:?matching Ubuntu AWS config file}")
out=$(realpath -m "${3:?separate build directory}")
patch_file=$(dirname "$(realpath "$0")")/damon-1018-region-size.patch
if patch --dry-run -s -N -d "$src" -p1 < "$patch_file"; then
    patch -d "$src" -p1 < "$patch_file"
else
    patch --dry-run -s -R -d "$src" -p1 < "$patch_file"
fi
mkdir -p "$out"
cp "$base" "$out/.config"
cfg=("$src/scripts/config" --file "$out/.config")
for feature in DAMON DAMON_VADDR DAMON_PADDR DAMON_SYSFS; do
    "${cfg[@]}" --enable "$feature"
done
# The test build has its own release string, module key and trust store.
"${cfg[@]}" --set-str LOCALVERSION "-pmt-damon" --disable LOCALVERSION_AUTO
"${cfg[@]}" --set-str MODULE_SIG_KEY "certs/signing_key.pem"
"${cfg[@]}" --set-str SYSTEM_TRUSTED_KEYS "" --set-str SYSTEM_REVOCATION_KEYS ""
# Omit DWARF/BTF to fit the local build disk; save the complete config delta.
"${cfg[@]}" --enable DEBUG_INFO_NONE --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
    --disable DEBUG_INFO_DWARF4 --disable DEBUG_INFO_DWARF5 --disable DEBUG_INFO_BTF
make -C "$src" O="$out" olddefconfig
for feature in DAMON DAMON_VADDR DAMON_PADDR DAMON_SYSFS; do
    grep -qx "CONFIG_$feature=y" "$out/.config"
done
"$src/scripts/diffconfig" "$base" "$out/.config" > "$out/pmt-config.diff"
make -C "$src" O="$out" -j"${JOBS:-6}" KDEB_PKGVERSION=7.0.0-1018.19+pmt1 bindeb-pkg
