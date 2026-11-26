#!/usr/bin/env bash
# Install two locally built packages on an Ubuntu EC2 test host. Use the custom
# kernel for one boot; an ordinary reboot selects the previously running AWS kernel.
set -euo pipefail
image=${1:?image deb}
headers=${2:?headers deb}
fallback=$(uname -r)
[[ $fallback == *-aws && -f /boot/vmlinuz-$fallback ]]
[[ $(mokutil --sb-state) == *"SecureBoot disabled"* ]]
release=$(dpkg-deb -f "$image" Package)
release=${release#linux-image-}
[[ $release == *-pmt-damon ]]
[[ $(dpkg-deb -f "$headers" Package) == "linux-headers-$release" ]]
dpkg -i "$image" "$headers"
for flag in DAMON DAMON_VADDR DAMON_SYSFS; do
    grep -qx "CONFIG_$flag=y" "/boot/config-$release"
done
apt-mark hold "linux-image-$release" "linux-headers-$release"
# Ubuntu's GRUB menu IDs include the filesystem UUID and are stable across menu updates.
boot_id=$(grub-probe --target=fs_uuid /)
submenu="gnulinux-advanced-$boot_id"
entry() { printf '%s>gnulinux-%s-advanced-%s' "$submenu" "$1" "$boot_id"; }
fallback_entry=$(entry "$fallback")
next_entry=$(entry "$release")
printf 'GRUB_DEFAULT="%s"\n' "$fallback_entry" > /etc/default/grub.d/99-pmt-fallback.cfg
update-grub
for version in "$fallback" "$release"; do
    grep -Fq "gnulinux-$version-advanced-$boot_id" /boot/grub/grub.cfg
done
test -s "/boot/initrd.img-$release"
grub-reboot "$next_entry"
grub-editenv list | grep -Fx "next_entry=$next_entry"
