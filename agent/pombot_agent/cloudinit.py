"""cloud-init NoCloud-Seed (ISO) für KVM-Gäste erzeugen."""
import json
from pathlib import Path

from . import netcfg
from .util import run


def hash_password(password: str) -> str:
    return run(["openssl", "passwd", "-6", "-stdin"], input_text=password + "\n").strip()


def build_seed(target_dir: Path, spec: dict, job=None) -> Path:
    """Schreibt user-data/meta-data/network-config und packt sie in seed.iso."""
    hostname = spec.get("hostname") or spec["name"]
    short = hostname.split(".")[0]
    password = spec.get("password") or ""
    keys = [k.strip() for k in (spec.get("ssh_keys") or []) if k.strip()]

    root = {"name": "root", "lock_passwd": not password}
    if password:
        root["hashed_passwd"] = hash_password(password)
    if keys:
        root["ssh_authorized_keys"] = keys

    sshd = "PermitRootLogin yes\nPasswordAuthentication yes\n" if password else \
        "PermitRootLogin prohibit-password\n"
    user_data = {
        "hostname": short,
        "manage_etc_hosts": True,
        "disable_root": False,
        "ssh_pwauth": bool(password),
        "users": [root],
        "package_update": True,
        "packages": ["qemu-guest-agent"],
        "write_files": [{
            "path": "/etc/ssh/sshd_config.d/01-pombot.conf",
            "content": sshd,
            "permissions": "0644",
        }],
        "runcmd": [
            ["systemctl", "enable", "--now", "qemu-guest-agent"],
            ["sh", "-c", "systemctl restart ssh || systemctl restart sshd || true"],
        ],
    }
    if "." in hostname:
        user_data["fqdn"] = hostname

    network = {
        "version": 2,
        "ethernets": {
            "eth0": netcfg.v2_ethernet(spec.get("ips") or [], spec.get("dns") or [],
                                       match={"macaddress": spec["mac"]}),
        },
    }
    meta = {"instance-id": f"{spec['name']}-{spec.get('instance_suffix', '1')}", "local-hostname": short}

    seed_dir = target_dir / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    # JSON ist gültiges YAML – so brauchen wir keine YAML-Bibliothek.
    (seed_dir / "user-data").write_text("#cloud-config\n" + json.dumps(user_data, indent=2) + "\n")
    (seed_dir / "meta-data").write_text(json.dumps(meta, indent=2) + "\n")
    (seed_dir / "network-config").write_text(json.dumps(network, indent=2) + "\n")
    (seed_dir / "user-data").chmod(0o600)

    iso = target_dir / "seed.iso"
    if job:
        job.write("Erzeuge cloud-init Seed (IP, Passwort, SSH-Keys) …")
    run(["xorriso", "-as", "mkisofs", "-output", iso, "-volid", "cidata", "-joliet", "-rock",
         seed_dir / "user-data", seed_dir / "meta-data", seed_dir / "network-config"])
    iso.chmod(0o644)
    return iso
