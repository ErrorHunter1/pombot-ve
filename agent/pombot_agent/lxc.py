"""LXC-Container verwalten (lxc-* Werkzeuge)."""
import json
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from . import netcfg, storage
from .config import BACKUP_DIR, GUEST_DIR, LXC_BACKEND, LXC_KEYSERVER, LXC_PATH, LXC_UNPRIVILEGED
from .util import CmdError, check_name, check_snap, ok, run

_cpu_prev: dict[str, tuple[float, int]] = {}
ATTACH = ["--clear-env", "--set-var", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
          "--set-var", "DEBIAN_FRONTEND=noninteractive"]


def _dir(name: str) -> Path:
    return LXC_PATH / check_name(name)


def exists(name: str) -> bool:
    return (_dir(name) / "config").exists()


def list_guests() -> dict:
    result = {}
    out = run(["lxc-ls", "-f", "-F", "NAME,STATE"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^pv\d+$", parts[0]):
            result[parts[0]] = {"type": "lxc", "state": _state(parts[1])}
    return result


def _state(raw: str) -> str:
    return {"RUNNING": "running", "STOPPED": "stopped", "FROZEN": "paused",
            "STARTING": "starting", "STOPPING": "stopping"}.get(raw.strip().upper(), "unknown")


def state(name: str) -> str:
    out = run(["lxc-info", "-n", name, "-s", "-H"])
    return _state(out.strip())


# ---------------------------------------------------------------- Konfiguration

def _read_config(name: str) -> list[str]:
    return (_dir(name) / "config").read_text().splitlines()


def get_config(name: str, key: str) -> str | None:
    for line in _read_config(name):
        if "=" in line and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip()
    return None


def set_config(name: str, key: str, value: str | None) -> None:
    lines = [ln for ln in _read_config(name) if not ("=" in ln and ln.split("=", 1)[0].strip() == key)]
    if value is not None:
        lines.append(f"{key} = {value}")
    (_dir(name) / "config").write_text("\n".join(lines) + "\n")


def _rootfs(name: str) -> tuple[str, Path]:
    raw = get_config(name, "lxc.rootfs.path") or ""
    if raw.startswith("loop:"):
        return "loop", Path(raw[5:])
    if raw.startswith("dir:"):
        raw = raw[4:]
    return "dir", Path(raw or _dir(name) / "rootfs")


def nesting_conf() -> list[str]:
    """„Nesting“ wie bei Proxmox: erlaubt systemd im Container eigene Namespaces für Dienste mit Sandboxing
    (z. B. Redis, MariaDB – sonst 226/NAMESPACE). LXC erzeugt dafür selbst ein passendes AppArmor-Profil."""
    enabled = Path("/sys/module/apparmor/parameters/enabled")
    try:
        apparmor = enabled.read_text().strip().upper().startswith("Y")
    except OSError:
        apparmor = False
    if apparmor and not shutil.which("apparmor_parser"):
        return []  # generiertes Profil ließe sich nicht laden – lieber ohne Nesting als gar nicht starten
    return ["lxc.apparmor.profile = generated", "lxc.apparmor.allow_nesting = 1"]


def _limits(cores: int, memory_mb: int) -> dict:
    return {"lxc.cgroup2.cpu.max": f"{int(cores) * 100000} 100000",
            "lxc.cgroup2.memory.max": f"{int(memory_mb)}M",
            "lxc.cgroup2.memory.swap.max": f"{int(memory_mb)}M"}


def attach(name: str, command: str, job=None, input_text=None, timeout=900, log=True) -> str:
    return run(["lxc-attach", "-n", name, *ATTACH, "--", "/bin/sh", "-c", command],
               job=job, input_text=input_text, timeout=timeout, log=log)


def _wait_running(name: str, timeout: int = 60) -> None:
    run(["lxc-wait", "-n", name, "-s", "RUNNING", "-t", str(timeout)], timeout=timeout + 10)


def _start(name: str, job=None) -> None:
    # Eigene systemd-Unit mit Delegate=yes, damit der Container eine saubere cgroup bekommt
    # und nicht beim Neustart des Agents mit beendet wird.
    unit = f"pombot-lxc-{name}"
    run(["systemctl", "reset-failed", unit], check=False)
    run(["systemd-run", f"--unit={unit}", "--collect", "--property=Delegate=yes",
         "--property=KillMode=mixed", "--property=TimeoutStopSec=90",
         "lxc-start", "-F", "-n", name], job=job, timeout=60)
    _wait_running(name)


def _stop(name: str, job=None, timeout: int = 60) -> None:
    if state(name) != "stopped":
        run(["lxc-stop", "-n", name, "-t", str(timeout)], job=job, check=False, timeout=timeout + 30)
        if state(name) != "stopped":
            run(["lxc-stop", "-n", name, "-k"], job=job, check=False)
    _wait_released(name)


def _wait_released(name: str, timeout: int = 60) -> None:
    """Wartet, bis lxc-start beendet und die Loop-Disk ausgehängt ist. Erst dann darf die
    Disk kopiert werden, sonst ändert sie sich noch während des Lesens."""
    unit = f"pombot-lxc-{name}"
    kind, path = _rootfs(name)
    deadline = time.time() + timeout
    while time.time() < deadline:
        unit_active = ok(["systemctl", "is-active", "--quiet", unit])
        loop_busy = kind == "loop" and bool(run(["losetup", "-j", path], check=False).strip())
        if not unit_active and not loop_busy:
            break
        time.sleep(1)
    run(["sync"], check=False)


# ---------------------------------------------------------------- Anlegen

def _set_net_config(name: str, ips: list[dict]) -> None:
    """IP-Adressen zusätzlich über LXC setzen: Sie sind dann schon beim Start aktiv – unabhängig davon,
    welches Netzwerksystem die Distribution im Container verwendet."""
    lines = [ln for ln in _read_config(name)
             if not ln.split("=", 1)[0].strip().startswith(("lxc.net.0.ipv4.", "lxc.net.0.ipv6."))]
    for ip in ips:
        fam = "ipv4" if ip["version"] == 4 else "ipv6"
        lines.append(f"lxc.net.0.{fam}.address = {ip['address']}/{ip['prefix']}")
        if ip.get("gateway"):
            lines.append(f"lxc.net.0.{fam}.gateway = {ip['gateway']}")
    (_dir(name) / "config").write_text("\n".join(lines) + "\n")


def _network_script(spec: dict) -> str:
    ips, dns = spec.get("ips") or [], spec.get("dns") or []
    hostname = spec.get("hostname") or spec["name"]
    short = hostname.split(".")[0]
    netplan = json.dumps({"network": {"version": 2, "ethernets": {"eth0": netcfg.v2_ethernet(ips, dns)}}},
                         indent=2)
    networkd = netcfg.networkd_unit(ips, dns)
    ifupdown = netcfg.ifupdown(ips, dns)
    resolv = "".join(f"nameserver {s}\n" for s in dns)
    q = shlex.quote
    return f"""set -e
echo {q(short)} > /etc/hostname
hostname {q(short)} 2>/dev/null || true
sed -i '/^127\\.0\\.1\\.1/d' /etc/hosts
echo {q(f"127.0.1.1 {hostname} {short}")} >> /etc/hosts
if [ -d /etc/netplan ]; then
  rm -f /etc/netplan/*.yaml
  printf '%s\\n' {q(netplan)} > /etc/netplan/50-pombot.yaml
  chmod 600 /etc/netplan/50-pombot.yaml
  netplan apply || true
elif [ -d /etc/systemd/network ] && systemctl is-enabled systemd-networkd >/dev/null 2>&1; then
  rm -f /etc/systemd/network/eth0.network
  printf '%s' {q(networkd)} > /etc/systemd/network/10-pombot-eth0.network
  systemctl restart systemd-networkd || true
else
  printf '%s' {q(ifupdown)} > /etc/network/interfaces
  (ifdown eth0 || true; ifup eth0 || true) 2>/dev/null
fi
# DNS: eigene resolv.conf, außer systemd-resolved läuft tatsächlich (dann kommt DNS aus netplan/networkd)
if [ -n {q(resolv)} ] && {{ [ ! -L /etc/resolv.conf ] || ! systemctl is-active -q systemd-resolved 2>/dev/null; }}; then
  rm -f /etc/resolv.conf; printf '%s' {q(resolv)} > /etc/resolv.conf
fi
"""


def _ssh_script(spec: dict) -> str:
    keys = "\n".join(k.strip() for k in (spec.get("ssh_keys") or []) if k.strip())
    permit = "yes" if spec.get("password") else "prohibit-password"
    q = shlex.quote
    return f"""set -e
APT="-o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=1"
for i in 1 2 3 4; do
  if command -v apt-get >/dev/null; then
    timeout 240 apt-get $APT update -qq 2>&1 | tail -n 3
    timeout 300 apt-get $APT install -y -qq openssh-server >/dev/null 2>/tmp/pombot-apt.err && break
    tail -n 3 /tmp/pombot-apt.err 2>/dev/null || true
  elif command -v apk >/dev/null; then
    timeout 300 apk add --no-cache openssh >/dev/null && break
  elif command -v dnf >/dev/null; then
    timeout 300 dnf install -y -q openssh-server >/dev/null && break
  fi
  echo "Paketinstallation fehlgeschlagen (Versuch $i/4), neuer Versuch in 5s …"; sleep 5
done
if ! command -v sshd >/dev/null && [ ! -x /usr/sbin/sshd ]; then
  echo "WARNUNG: openssh-server konnte nicht installiert werden – hat der Container Internet?"
  echo "Netzwerk-Diagnose:"; ip -4 addr show eth0 2>&1 | grep inet; ip route 2>&1; cat /etc/resolv.conf 2>&1
fi
mkdir -p /etc/ssh/sshd_config.d /root/.ssh
chmod 700 /root/.ssh
printf 'PermitRootLogin {permit}\\nPasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/01-pombot.conf
if [ -n {q(keys)} ]; then printf '%s\\n' {q(keys)} >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys; fi
(systemctl enable ssh 2>/dev/null; systemctl restart ssh 2>/dev/null) || (rc-update add sshd 2>/dev/null; rc-service sshd restart 2>/dev/null) || true
"""


def _log_output(job, output: str) -> None:
    for line in output.strip().splitlines()[-25:]:
        job.write(f"  {line}")


def create(job, spec: dict) -> dict:
    name = check_name(spec["name"])
    if exists(name):
        raise CmdError(f"Container {name} existiert bereits")
    arch = run(["dpkg", "--print-architecture"]).strip() or "amd64"
    base_conf = [
        "lxc.net.0.type = veth",
        f"lxc.net.0.link = {spec['bridge']}",
        "lxc.net.0.flags = up",
        f"lxc.net.0.hwaddr = {spec['mac']}",
        "lxc.net.0.name = eth0",
    ]
    if LXC_UNPRIVILEGED:
        base_conf += ["lxc.idmap = u 0 100000 65536", "lxc.idmap = g 0 100000 65536"]
    base_conf += nesting_conf()
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
        fh.write("\n".join(base_conf) + "\n")
        conf_path = fh.name
    cmd = ["lxc-create", "-n", name, "-f", conf_path, "-t", "download"]
    if LXC_BACKEND == "loop":
        cmd += ["-B", "loop", "--fssize", f"{int(spec['disk_gb'])}G"]
    cmd += ["--", "-d", spec["lxc_dist"], "-r", spec["lxc_release"], "-a", arch]
    try:
        job.write(f"Lade Container-Image {spec['lxc_dist']} {spec['lxc_release']} ({arch}) …")
        run(cmd, job=job, timeout=3600, env={"DOWNLOAD_KEYSERVER": LXC_KEYSERVER})
        if spec.get("storage_id"):
            # Container-Verzeichnis (Konfiguration + rootdev) auf den gemeinsamen Speicher verschieben
            target = storage.prepare_guest_dir(spec["storage_id"], name) / "lxc"
            job.write(f"Verschiebe Container auf gemeinsamen Speicher: {target}")
            shutil.move(str(_dir(name)), target)
            (LXC_PATH / name).symlink_to(target)
        for key, value in _limits(spec["cores"], spec["memory_mb"]).items():
            set_config(name, key, value)
        _set_net_config(name, spec.get("ips") or [])
        set_config(name, "lxc.start.auto", "1" if spec.get("onboot", True) else "0")
        set_config(name, "lxc.uts.name", (spec.get("hostname") or name).split(".")[0])
        job.write("Starte Container …")
        _start(name, job)
        time.sleep(3)
        job.write("Konfiguriere Netzwerk (IP, Gateway, DNS) …")
        _log_output(job, attach(name, _network_script(spec), job=job, log=False))
        if spec.get("password"):
            job.write("Setze root-Passwort …")
            run(["lxc-attach", "-n", name, *ATTACH, "--", "chpasswd"],
                input_text=f"root:{spec['password']}\n", log=False)
        job.write("Installiere und konfiguriere SSH …")
        _log_output(job, attach(name, _ssh_script(spec), job=job, log=False, timeout=1200))
        if spec.get("app_script"):
            from . import apps
            job.write("Starte Installation der Anwendung …")
            apps.start_in_container(name, spec["app_script"], job)
    except Exception:
        job.write("Räume nach Fehler auf …")
        run(["lxc-stop", "-n", name, "-k"], check=False)
        if not (LXC_PATH / name).is_symlink():
            run(["lxc-destroy", "-n", name, "-f"], check=False)
        storage.remove_guest_dirs(name)
        raise
    finally:
        Path(conf_path).unlink(missing_ok=True)
    return {"state": state(name)}


def delete(job, name: str) -> dict:
    job.write(f"Lösche Container {name} …")
    if exists(name):
        run(["lxc-stop", "-n", name, "-k"], check=False)
        _wait_released(name, timeout=30)
        if not (LXC_PATH / name).is_symlink():  # Speicher-Container: Verzeichnis selbst löschen
            run(["lxc-destroy", "-n", name, "-f"], job=job)
    storage.remove_guest_dirs(name)
    return {}


def action(name: str, act: str) -> str:
    if act == "start":
        _start(name)
    elif act == "stop":
        run(["lxc-stop", "-n", name, "-k"], timeout=60)
    elif act == "shutdown":
        run(["lxc-stop", "-n", name, "-t", "60"], timeout=90)
    elif act == "reboot":
        run(["lxc-stop", "-n", name, "-r", "-t", "60"], timeout=90)
    elif act == "suspend":
        run(["lxc-freeze", "-n", name])
    elif act == "resume":
        run(["lxc-unfreeze", "-n", name])
    else:
        raise CmdError("Unbekannte Aktion")
    return state(name)


def config(name: str) -> dict:
    cpu = (get_config(name, "lxc.cgroup2.cpu.max") or "").split()
    cores = int(cpu[0]) // int(cpu[1]) if len(cpu) == 2 and cpu[0].isdigit() else 0
    mem = get_config(name, "lxc.cgroup2.memory.max") or ""
    memory_mb = int(mem[:-1]) if mem.endswith("M") and mem[:-1].isdigit() else 0
    return {"cores": cores, "memory_mb": memory_mb}


def status(name: str) -> dict:
    st = state(name)
    cfg = config(name)
    result = {"type": "lxc", "state": st, **cfg, "cpu": 0.0, "memory_used": None,
              "disk_used": None, "disk_total": None, "net_rx": 0, "net_tx": 0}
    if st not in ("running", "paused"):
        return result
    kv = {}
    for line in run(["lxc-info", "-n", name, "-H"]).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip()] = v.strip()
    now = time.time()
    cpu_ns = int(kv.get("CPU use", "0") or 0)
    prev = _cpu_prev.get(name)
    _cpu_prev[name] = (now, cpu_ns)
    if prev and now > prev[0] and cfg["cores"]:
        result["cpu"] = round(max(0.0, (cpu_ns - prev[1]) / ((now - prev[0]) * 1e9 * cfg["cores"]) * 100), 1)
    if kv.get("Memory use", "").isdigit():
        result["memory_used"] = int(kv["Memory use"])
    else:  # cgroup v2: lxc-info liefert den Wert nicht immer
        try:
            result["memory_used"] = int(run(["lxc-cgroup", "-n", name, "memory.current"], timeout=10).strip())
        except (CmdError, ValueError):
            pass
    if kv.get("RX bytes", "").isdigit():
        result["net_rx"] = int(kv["RX bytes"])
        result["net_tx"] = int(kv.get("TX bytes", "0"))
    try:
        parts = attach(name, "df -B1 -P / | tail -1", timeout=10, log=False).split()
        result["disk_total"], result["disk_used"] = int(parts[1]), int(parts[2])
    except (CmdError, IndexError, ValueError):
        pass
    return result


def resize(job, name: str, cores: int | None, memory_mb: int | None, disk_gb: int | None) -> dict:
    cfg = config(name)
    limits = _limits(cores or cfg["cores"] or 1, memory_mb or cfg["memory_mb"] or 512)
    running = state(name) in ("running", "paused")
    if cores or memory_mb:
        for key, value in limits.items():
            set_config(name, key, value)
            if running:
                run(["lxc-cgroup", "-n", name, key.split(".", 2)[2], value.replace('"', "")],
                    job=job, check=False)
        job.write("CPU/RAM-Limits sofort aktiv.")
    if disk_gb:
        kind, path = _rootfs(name)
        if kind != "loop":
            raise CmdError("Festplattengröße kann nur bei Loop-Containern geändert werden")
        new_size = int(disk_gb) * (1 << 30)
        if new_size < path.stat().st_size:
            raise CmdError("Festplatten können nur vergrößert werden")
        if new_size > path.stat().st_size:
            if running:
                job.write("Container wird für die Vergrößerung kurz gestoppt …")
                _stop(name, job)
            run(["truncate", "-s", str(new_size), path], job=job)
            run(["e2fsck", "-fy", path], job=job, check=False)
            run(["resize2fs", path], job=job)
            if running:
                _start(name, job)
    return {}


def set_network(job, name: str, spec: dict) -> dict:
    """Netzwerk ändern (IPs, MAC, Bridge). Neustart des Containers, danach wird das Netzwerk im
    Container neu geschrieben – Passwort und Daten bleiben erhalten."""
    was_running = state(name) in ("running", "paused")
    if was_running:
        _stop(name, job)
    job.write(f"Netzwerkkarte: MAC {get_config(name, 'lxc.net.0.hwaddr')} → {spec['mac']}, "
              f"Bridge {get_config(name, 'lxc.net.0.link')} → {spec['bridge']}")
    set_config(name, "lxc.net.0.hwaddr", spec["mac"])
    set_config(name, "lxc.net.0.link", spec["bridge"])
    _set_net_config(name, spec.get("ips") or [])
    _start(name, job)
    time.sleep(2)
    job.write("Schreibe Netzwerk-Konfiguration im Container …")
    _log_output(job, attach(name, _network_script(spec), job=job, log=False))
    if not was_running:
        _stop(name, job)
    return {"state": state(name)}


def set_password(name: str, user: str, password: str) -> None:
    if state(name) != "running":
        raise CmdError("Der Container muss laufen")
    run(["lxc-attach", "-n", name, *ATTACH, "--", "chpasswd"], input_text=f"{user}:{password}\n", log=False)


# ---------------------------------------------------------------- Snapshots (eigene Umsetzung)

def _snap_dir(name: str, snap: str | None = None) -> Path:
    base = GUEST_DIR / check_name(name) / "snapshots"
    return base / check_snap(snap) if snap else base


def snapshots(name: str) -> list[dict]:
    base = _snap_dir(name)
    result = []
    if base.exists():
        for d in sorted(base.iterdir()):
            meta_file = d / "meta.json"
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                result.append({"name": d.name, "created": time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(meta["created"])), "state": "stopped",
                               "description": meta.get("description", "")})
    return result


def _copy_rootfs(name: str, dest: Path, job, restore: bool = False) -> None:
    kind, path = _rootfs(name)
    if kind == "loop":
        src, dst = (dest / "rootdev", path) if restore else (path, dest / "rootdev")
        run(["cp", "--sparse=always", src, dst], job=job, timeout=6 * 3600)
    elif restore:
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
        run(["tar", "--numeric-owner", "-xpf", dest / "rootfs.tar", "-C", path], job=job, timeout=6 * 3600)
    else:
        run(["tar", "--numeric-owner", "-cpf", dest / "rootfs.tar", "-C", path, "."], job=job, timeout=6 * 3600)


def snapshot_create(job, name: str, snap: str, description: str = "") -> dict:
    dest = _snap_dir(name, snap)
    if dest.exists():
        raise CmdError("Snapshot existiert bereits")
    was_running = state(name) in ("running", "paused")
    if was_running:
        job.write("Container wird für den Snapshot kurz gestoppt …")
        _stop(name, job)
    try:
        dest.mkdir(parents=True)
        _copy_rootfs(name, dest, job)
        shutil.copy2(_dir(name) / "config", dest / "config")
        (dest / "meta.json").write_text(json.dumps({"created": time.time(), "description": description}))
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        if was_running:
            _start(name, job)
    return {}


def snapshot_delete(job, name: str, snap: str) -> dict:
    dest = _snap_dir(name, snap)
    if not dest.exists():
        raise CmdError("Snapshot nicht gefunden")
    shutil.rmtree(dest)
    job.write(f"Snapshot {snap} gelöscht.")
    return {}


def snapshot_rollback(job, name: str, snap: str) -> dict:
    dest = _snap_dir(name, snap)
    if not dest.exists():
        raise CmdError("Snapshot nicht gefunden")
    was_running = state(name) in ("running", "paused")
    _stop(name, job)
    _copy_rootfs(name, dest, job, restore=True)
    shutil.copy2(dest / "config", _dir(name) / "config")
    if was_running:
        _start(name, job)
    return {"state": state(name)}


# ---------------------------------------------------------------- Backups

def backup(job, name: str, auto: bool = False) -> dict:
    ts = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{name}-lxc-{ts}{'-auto' if auto else ''}.tar.gz"
    was_running = state(name) in ("running", "paused")
    if was_running:
        job.write("Container wird für ein konsistentes Backup gestoppt (Stop-Modus) …")
        _stop(name, job)
    real = _dir(name).resolve()  # bei gemeinsamem Speicher: Inhalt statt Symlink sichern
    try:
        run(["tar", "--numeric-owner", "-S", "-czpf", target, "--transform", f"s,^{real.name}(/|$),{name}\\1,x",
             "-C", real.parent, real.name], job=job, timeout=6 * 3600)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if was_running:
            _start(name, job)
    return {"file": target.name, "size": target.stat().st_size}


def restore(job, name: str, archive: Path) -> dict:
    listing = run(["tar", "-tzf", archive], timeout=3600).splitlines()
    if not listing or any(not (p == name or p.startswith(name + "/")) for p in listing):
        raise CmdError("Backup passt nicht zu diesem Container")
    # Ziel ist das echte Verzeichnis – bei gemeinsamem Speicher bleibt der Container dort (Symlink bleibt)
    real = _dir(name).resolve()
    stage = real.parent / f".{name}.restore-new"
    old = real.parent / f".{name}.restore-old"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        run(["tar", "--numeric-owner", "-xzpf", archive, "-C", stage], job=job, timeout=6 * 3600)
        if exists(name):
            _stop(name, job)
        if real.exists():
            shutil.rmtree(old, ignore_errors=True)
            real.rename(old)
        (stage / name).rename(real)
    except Exception:
        if old.exists() and not real.exists():
            old.rename(real)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    from . import ha
    if name in ha.ha_guests():
        set_config(name, "lxc.start.auto", "0")
    _start(name, job)
    return {"state": state(name)}
