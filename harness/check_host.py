"""Report Linux host readiness without changing kernel or storage settings."""

import argparse
import gzip
import json
import os
from pathlib import Path
import platform
import re
import stat


FEATURES = (
    "CONFIG_DAMON", "CONFIG_DAMON_VADDR", "CONFIG_DAMON_SYSFS",
    "CONFIG_IDLE_PAGE_TRACKING", "CONFIG_LRU_GEN", "CONFIG_SWAP",
    "CONFIG_TRANSPARENT_HUGEPAGE", "CONFIG_DEBUG_FS",
)
CONFIG_KEYS = (*FEATURES, "CONFIG_ZSWAP")
NVME_INSTANCE_STORE = "Amazon EC2 NVMe Instance Storage"


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def kernel_config(release):
    sources = (Path(f"/boot/config-{release}"), Path("/proc/config.gz"),
               Path(f"/lib/modules/{release}/build/.config"))
    for source in sources:
        try:
            config = gzip.open(source, "rt") if source.suffix == ".gz" else source.open()
            with config:
                features = {}
                for line in config:
                    if "=" in line and line.startswith("CONFIG_"):
                        key, value = line.strip().split("=", 1)
                        if key in CONFIG_KEYS:
                            features[key] = value
                return str(source), {key: features.get(key, "n") for key in CONFIG_KEYS}
        except OSError:
            continue
    return None, dict.fromkeys(CONFIG_KEYS)


def storage_model(source):
    try:
        info = Path(source).stat()
        if not stat.S_ISBLK(info.st_mode):
            return None
        device = Path(f"/sys/dev/block/{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")
        node = device.resolve(strict=True)
    except OSError:
        return None
    if (node / "partition").exists():
        node = node.parent
    if not re.fullmatch(r"nvme\d+n\d+", node.name):
        return None
    return read(node / "device/model")


def swaps():
    lines = read("/proc/swaps")
    if lines is None:
        return None
    active = []
    for line in lines.splitlines()[1:]:
        source, kind, size_kib, used_kib, priority = line.split()
        active.append({
            "source": source, "type": kind, "size_kib": int(size_kib),
            "used_kib": int(used_kib), "priority": int(priority),
            "nvme_model": storage_model(source),
        })
    return active


def secure_boot():
    firmware = Path("/sys/firmware/efi")
    if not firmware.is_dir():
        return "no_efi"
    variables = list((firmware / "efivars").glob("SecureBoot-*"))
    if len(variables) == 1:
        try:
            raw = variables[0].read_bytes()
            if len(raw) == 5 and raw[4] in (0, 1):
                return "enabled" if raw[4] else "disabled"
        except OSError:
            pass
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("development", "nvme-test"), required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    release = platform.release()
    source, features = kernel_config(release)
    active_swap = swaps()
    report = {
        "schema_version": 1,
        "tier": args.tier,
        "os_release": read("/etc/os-release"),
        "kernel": release,
        "architecture": platform.machine(),
        "page_bytes": os.sysconf("SC_PAGE_SIZE"),
        "config_source": source,
        "features": features,
        "matching_headers": Path(f"/lib/modules/{release}/build/Makefile").is_file(),
        "cgroup_controllers": (read("/sys/fs/cgroup/cgroup.controllers") or "").split(),
        "thp": read("/sys/kernel/mm/transparent_hugepage/enabled"),
        "mglru": read("/sys/kernel/mm/lru_gen/enabled"),
        "zswap": read("/sys/module/zswap/parameters/enabled"),
        "crash_kernel_loaded": read("/sys/kernel/kexec_crash_loaded"),
        "kernel_taint": read("/proc/sys/kernel/tainted"),
        "secure_boot": secure_boot(),
        "active_swap": active_swap,
    }
    blockers = []
    if report["architecture"] != "x86_64" or report["page_bytes"] != 4096:
        blockers.append("expected x86_64 with 4 KiB pages")
    if not report["matching_headers"]:
        blockers.append("running-kernel headers are missing")
    if "memory" not in report["cgroup_controllers"]:
        blockers.append("cgroup v2 memory controller is unavailable at /sys/fs/cgroup")
    for feature in FEATURES:
        if features[feature] not in {"y", "m"}:
            blockers.append(f"{feature} is unavailable or unverified")
    if args.tier == "nvme-test":
        if features["CONFIG_DAMON"] == "m" or features["CONFIG_DAMON_VADDR"] == "m":
            blockers.append("verify that DAMON modules load before an NVMe run")
        if report["zswap"] not in {"N", "0"} and features["CONFIG_ZSWAP"] != "n":
            blockers.append("zswap must be confirmed disabled")
        if not active_swap or any(swap["nvme_model"] != NVME_INSTANCE_STORE
                                  for swap in active_swap):
            blockers.append("active swap must be exclusively on EC2 instance-store NVMe")
    report["blockers"] = blockers
    output = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return bool(blockers)


if __name__ == "__main__":
    raise SystemExit(main())
