"""Informationen und Auslastung des Hosts."""
import os
import platform
import shutil
import socket
import time
from pathlib import Path

import psutil

from .config import DATA_DIR, VERSION
from .util import run


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unbekannt"


def bridges() -> list[str]:
    net = Path("/sys/class/net")
    if not net.exists():
        return []
    return sorted(p.name for p in net.iterdir() if (p / "bridge").exists())


def _version(cmd) -> str | None:
    try:
        out = run(cmd, timeout=10).strip()
        return out.splitlines()[0] if out else None
    except Exception:  # noqa: BLE001
        return None


def _main_ipv4() -> str | None:
    try:
        parts = run(["ip", "-4", "route", "get", "1.1.1.1"], timeout=10).split()
        return parts[parts.index("src") + 1]
    except Exception:  # noqa: BLE001
        return None


def info() -> dict:
    du = shutil.disk_usage(DATA_DIR)
    return {
        "hostname": socket.gethostname(),
        "os": _os_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_cores": psutil.cpu_count(logical=True),
        "memory_total": psutil.virtual_memory().total,
        "disk_total": du.total,
        "kvm": os.path.exists("/dev/kvm"),
        # "none" = echte Hardware, sonst z. B. "kvm"/"vmware": Node ist selbst virtuell (Nested Virtualization)
        "virtualization": (_version(["systemd-detect-virt"]) or "none"),
        "libvirt": _version(["virsh", "--version"]),
        "lxc": _version(["lxc-start", "--version"]),
        "bridges": bridges(),
        "main_ipv4": _main_ipv4(),
        "agent_version": VERSION,
    }


def stats() -> dict:
    vm = psutil.virtual_memory()
    du = shutil.disk_usage(DATA_DIR)
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "load": list(os.getloadavg()),
        "memory_total": vm.total,
        "memory_used": vm.total - vm.available,
        "swap_total": psutil.swap_memory().total,
        "swap_used": psutil.swap_memory().used,
        "disk_total": du.total,
        "disk_used": du.used,
        "uptime": int(time.time() - psutil.boot_time()),
        "net_rx": net.bytes_recv,
        "net_tx": net.bytes_sent,
        "time": time.time(),
    }
