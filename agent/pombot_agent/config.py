"""Agent-Konfiguration. Wird aus /etc/pombot/agent.env bzw. Umgebungsvariablen gelesen."""
import ipaddress
import os
from pathlib import Path

VERSION = "0.2.0"


def _load_env(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(os.environ.get("POMBOT_AGENT_ENV", "/etc/pombot/agent.env"))

TOKEN = os.environ.get("POMBOT_AGENT_TOKEN", "")
DATA_DIR = Path(os.environ.get("POMBOT_DATA_DIR", "/var/lib/pombot"))
IMAGE_DIR = DATA_DIR / "images"
ISO_DIR = DATA_DIR / "iso"
GUEST_DIR = DATA_DIR / "guests"
BACKUP_DIR = DATA_DIR / "backups"
LXC_PATH = Path(os.environ.get("POMBOT_LXC_PATH", "/var/lib/lxc"))
LXC_BACKEND = os.environ.get("POMBOT_LXC_BACKEND", "loop")  # loop | dir
LXC_UNPRIVILEGED = os.environ.get("POMBOT_LXC_UNPRIVILEGED", "1") == "1"
LXC_KEYSERVER = os.environ.get("POMBOT_LXC_KEYSERVER", "hkp://keyserver.ubuntu.com:80")


def _parse_allow(raw: str):
    nets = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if item:
            nets.append(ipaddress.ip_network(item, strict=False))
    return nets


# Erlaubte Quell-IPs (Panel). Leer = alle erlaubt (Token ist trotzdem Pflicht).
ALLOW_FROM = _parse_allow(os.environ.get("POMBOT_AGENT_ALLOW", ""))

for _d in (IMAGE_DIR, ISO_DIR, GUEST_DIR, BACKUP_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
