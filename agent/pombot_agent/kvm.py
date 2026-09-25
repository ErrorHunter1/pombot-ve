"""KVM/QEMU-Gäste über libvirt (virsh) verwalten."""
import json
import os
import re
import shutil
import tarfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

from . import cloudinit, images
from .config import BACKUP_DIR, GUEST_DIR
from .util import CmdError, check_name, check_snap, ok, run

BACKUP_SNAP = "pvbackup"
_cpu_prev: dict[str, tuple[float, int]] = {}


def _state(raw: str) -> str:
    raw = raw.strip().lower()
    if raw == "running":
        return "running"
    if raw in ("paused", "pmsuspended"):
        return "paused"
    if raw in ("shut off", "shutoff", "crashed"):
        return "stopped"
    if "shutdown" in raw:
        return "stopping"
    return raw or "unknown"


def exists(name: str) -> bool:
    return ok(["virsh", "dominfo", name])


def list_guests() -> dict:
    out = run(["virsh", "list", "--all"])
    result = {}
    for line in out.splitlines()[2:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and re.match(r"^pv\d+$", parts[1]):
            result[parts[1]] = {"type": "kvm", "state": _state(parts[2])}
    return result


def state(name: str) -> str:
    return _state(run(["virsh", "domstate", name]))


def _guest_dir(name: str) -> Path:
    return GUEST_DIR / check_name(name)


def domain_xml(spec: dict, disk: Path, cdrom: Path | None, kvm: bool) -> str:
    cdrom_xml = ""
    if cdrom:
        cdrom_xml = f"""
    <disk type='file' device='cdrom' snapshot='no'>
      <driver name='qemu' type='raw'/>
      <source file='{escape(str(cdrom))}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>"""
    cpu = "<cpu mode='host-passthrough' check='none'/>" if kvm else ""
    return f"""<domain type='{"kvm" if kvm else "qemu"}'>
  <name>{spec['name']}</name>
  <title>{escape(spec.get('hostname') or spec['name'])}</title>
  <memory unit='MiB'>{int(spec['memory_mb'])}</memory>
  <currentMemory unit='MiB'>{int(spec['memory_mb'])}</currentMemory>
  <vcpu placement='static'>{int(spec['cores'])}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
    <boot dev='cdrom'/>
  </os>
  <features><acpi/><apic/></features>
  {cpu}
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='{escape(str(disk))}'/>
      <target dev='vda' bus='virtio'/>
    </disk>{cdrom_xml}
    <interface type='bridge'>
      <mac address='{spec['mac']}'/>
      <source bridge='{escape(spec['bridge'])}'/>
      <model type='virtio'/>
    </interface>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <input type='tablet' bus='usb'/>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>
    <video><model type='vga'/></video>
    <memballoon model='virtio'><stats period='10'/></memballoon>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
  </devices>
</domain>
"""


def create(job, spec: dict) -> dict:
    name = check_name(spec["name"])
    if exists(name):
        raise CmdError(f"VM {name} existiert bereits")
    d = _guest_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o755)
    disk = d / "disk.qcow2"
    size_gb = int(spec["disk_gb"])
    try:
        if spec.get("iso_url"):
            cdrom = images.ensure_iso(spec["iso_url"], job)
            job.write(f"Erstelle leere Festplatte ({size_gb} GB) …")
            run(["qemu-img", "create", "-f", "qcow2", disk, f"{size_gb}G"], job=job)
        else:
            base = images.ensure_cloud_image(spec["image_url"], job)
            info = images.image_info(base)
            min_gb = -(-int(info["virtual-size"]) // (1 << 30))
            if size_gb < min_gb:
                raise CmdError(f"Das Image braucht mindestens {min_gb} GB Festplatte")
            job.write("Kopiere Image auf neue Festplatte …")
            run(["qemu-img", "convert", "-f", info.get("format", "qcow2"), "-O", "qcow2", base, disk],
                job=job, timeout=3600)
            run(["qemu-img", "resize", disk, f"{size_gb}G"], job=job)
            cdrom = cloudinit.build_seed(d, spec, job)
        kvm = os.path.exists("/dev/kvm")
        if not kvm:
            job.write("WARNUNG: /dev/kvm fehlt – VM läuft ohne Hardware-Beschleunigung (langsam).")
        xml_path = d / "domain.xml"
        xml_path.write_text(domain_xml(spec, disk, cdrom, kvm))
        run(["virsh", "define", xml_path], job=job)
        run(["virsh", "autostart", name], job=job)
        run(["virsh", "start", name], job=job)
    except Exception:
        job.write("Räume nach Fehler auf …")
        _remove(name)
        raise
    return {"state": state(name)}


def _remove(name: str) -> None:
    if exists(name):
        run(["virsh", "destroy", name], check=False)
        run(["virsh", "undefine", name, "--snapshots-metadata", "--managed-save"], check=False)
    shutil.rmtree(_guest_dir(name), ignore_errors=True)


def delete(job, name: str) -> dict:
    job.write(f"Lösche VM {name} …")
    _remove(name)
    return {}


def action(name: str, act: str) -> str:
    cmds = {
        "start": ["virsh", "start", name],
        "stop": ["virsh", "destroy", name],
        "shutdown": ["virsh", "shutdown", name],
        "reboot": ["virsh", "reboot", name],
        "suspend": ["virsh", "suspend", name],
        "resume": ["virsh", "resume", name],
    }
    if act not in cmds:
        raise CmdError("Unbekannte Aktion")
    run(cmds[act], timeout=120)
    return state(name)


def remove_serial_consoles() -> list[str]:
    """Entfernt die serielle Konsole aus bestehenden VMs (ältere PomBot-Versionen hatten eine).
    Bei verschachtelter Virtualisierung (Server ist selbst eine VM) ist die emulierte serielle
    Schnittstelle extrem langsam und bringt den Kernel der VM beim Booten zum Hängen.
    Wirkt ab dem nächsten Stoppen/Starten der VM."""
    changed = []
    for name in list_guests():
        try:
            root = _xml(name, inactive=True)
            devices = root.find("devices")
            found = [el for el in devices if el.tag in ("serial", "console")]
            if not found:
                continue
            for el in found:
                devices.remove(el)
            path = _guest_dir(name) / "domain.xml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ET.tostring(root, encoding="unicode"))
            run(["virsh", "define", path])
            changed.append(name)
        except (CmdError, ET.ParseError, OSError):
            continue
    return changed


def _xml(name: str, inactive: bool = False) -> ET.Element:
    cmd = ["virsh", "dumpxml", name] + (["--inactive"] if inactive else [])
    return ET.fromstring(run(cmd))


def _disk_path(root: ET.Element) -> str | None:
    for disk in root.findall("./devices/disk[@device='disk']"):
        src = disk.find("source")
        if src is not None:
            return src.get("file")
    return None


def config(name: str) -> dict:
    root = _xml(name, inactive=True)
    mem = root.find("memory")
    mem_kib = int(mem.text) * {"KiB": 1, "MiB": 1024, "GiB": 1 << 20}.get(mem.get("unit", "KiB"), 1)
    return {"cores": int(root.find("vcpu").text), "memory_mb": mem_kib // 1024, "disk": _disk_path(root)}


def status(name: str) -> dict:
    st = state(name)
    cfg = config(name)
    result = {"type": "kvm", "state": st, "cores": cfg["cores"], "memory_mb": cfg["memory_mb"],
              "cpu": 0.0, "memory_used": None, "disk_used": None, "disk_total": None,
              "net_rx": 0, "net_tx": 0}
    out = run(["virsh", "domstats", name, "--cpu-total", "--balloon", "--vcpu", "--interface", "--block"])
    kv = {}
    for line in out.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k] = v
    now = time.time()
    cpu_ns = int(kv.get("cpu.time", 0))
    vcpus = int(kv.get("vcpu.current", cfg["cores"]) or 1)
    prev = _cpu_prev.get(name)
    _cpu_prev[name] = (now, cpu_ns)
    if prev and st == "running" and now > prev[0]:
        result["cpu"] = round(max(0.0, (cpu_ns - prev[1]) / ((now - prev[0]) * 1e9 * vcpus) * 100), 1)
    if "balloon.available" in kv and "balloon.unused" in kv:
        result["memory_used"] = (int(kv["balloon.available"]) - int(kv["balloon.unused"])) * 1024
    elif "balloon.rss" in kv:
        result["memory_used"] = int(kv["balloon.rss"]) * 1024
    for i in range(int(kv.get("block.count", 0))):
        if kv.get(f"block.{i}.name") == "vda":
            result["disk_used"] = int(kv.get(f"block.{i}.allocation", 0))
            result["disk_total"] = int(kv.get(f"block.{i}.capacity", 0))
    for i in range(int(kv.get("net.count", 0))):
        result["net_rx"] += int(kv.get(f"net.{i}.rx.bytes", 0))
        result["net_tx"] += int(kv.get(f"net.{i}.tx.bytes", 0))
    return result


def resize(job, name: str, cores: int | None, memory_mb: int | None, disk_gb: int | None) -> dict:
    root = _xml(name, inactive=True)
    changed = False
    if cores:
        root.find("vcpu").text = str(int(cores))
        changed = True
    if memory_mb:
        for tag in ("memory", "currentMemory"):
            el = root.find(tag)
            if el is not None:
                el.set("unit", "MiB")
                el.text = str(int(memory_mb))
        changed = True
    if changed:
        path = _guest_dir(name) / "domain.xml"
        path.write_text(ET.tostring(root, encoding="unicode"))
        run(["virsh", "define", path], job=job)
        job.write("CPU/RAM geändert – wird nach Stoppen und Starten der VM aktiv.")
    if disk_gb:
        disk = _disk_path(root)
        current = images.image_info(Path(disk))["virtual-size"]
        if int(disk_gb) * (1 << 30) < current:
            raise CmdError("Festplatten können nur vergrößert werden")
        if int(disk_gb) * (1 << 30) > current:
            if state(name) in ("running", "paused"):
                run(["virsh", "blockresize", name, "vda", f"{int(disk_gb)}G"], job=job)
            else:
                run(["qemu-img", "resize", disk, f"{int(disk_gb)}G"], job=job)
            job.write("Festplatte vergrößert. Die Partition wächst beim nächsten Boot automatisch (cloud-init).")
    return {}


def set_password(name: str, user: str, password: str) -> None:
    if state(name) != "running":
        raise CmdError("Die VM muss laufen (qemu-guest-agent wird benötigt)")
    run(["virsh", "set-user-password", name, user, password], timeout=60)


def vnc_port(name: str) -> int:
    out = run(["virsh", "vncdisplay", name]).strip()
    if not out:
        raise CmdError("Keine VNC-Konsole verfügbar – läuft die VM?")
    return 5900 + int(out.rsplit(":", 1)[1])


# ---------------------------------------------------------------- Snapshots

def snapshots(name: str) -> list[dict]:
    out = run(["virsh", "snapshot-list", name])
    result = []
    for line in out.splitlines()[2:]:
        m = re.match(r"^\s*(\S+)\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\s*\S*)\s+(\S+)", line)
        if m and m.group(1) != BACKUP_SNAP:
            result.append({"name": m.group(1), "created": m.group(2).strip(), "state": m.group(3)})
    return result


def snapshot_create(job, name: str, snap: str, description: str = "") -> dict:
    check_snap(snap)
    run(["virsh", "snapshot-create-as", "--domain", name, "--name", snap,
         "--description", description or snap, "--atomic"], job=job, timeout=1800)
    return {}


def snapshot_delete(job, name: str, snap: str) -> dict:
    run(["virsh", "snapshot-delete", name, check_snap(snap)], job=job, timeout=1800)
    return {}


def snapshot_rollback(job, name: str, snap: str) -> dict:
    run(["virsh", "snapshot-revert", name, check_snap(snap)], job=job, timeout=1800)
    return {"state": state(name)}


# ---------------------------------------------------------------- Backups

def backup(job, name: str, auto: bool = False) -> dict:
    d = _guest_dir(name)
    disk = Path(config(name)["disk"])
    ts = time.strftime("%Y%m%d-%H%M%S")
    stage = BACKUP_DIR / f".stage-{name}-{ts}"
    stage.mkdir(parents=True)
    target = BACKUP_DIR / f"{name}-kvm-{ts}{'-auto' if auto else ''}.tar"
    try:
        if state(name) in ("running", "paused"):
            job.write("VM läuft – erstelle konsistenten Zwischen-Snapshot …")
            run(["virsh", "snapshot-delete", name, BACKUP_SNAP], check=False)
            run(["virsh", "snapshot-create-as", "--domain", name, "--name", BACKUP_SNAP, "--atomic"],
                job=job, timeout=1800)
            try:
                run(["qemu-img", "convert", "-U", "-O", "qcow2", "-c", "-l", f"snapshot.name={BACKUP_SNAP}",
                     disk, stage / "disk.qcow2"], job=job, timeout=6 * 3600)
            finally:
                run(["virsh", "snapshot-delete", name, BACKUP_SNAP], check=False)
        else:
            run(["qemu-img", "convert", "-O", "qcow2", "-c", disk, stage / "disk.qcow2"],
                job=job, timeout=6 * 3600)
        (stage / "domain.xml").write_text(run(["virsh", "dumpxml", "--inactive", name]))
        if (d / "seed.iso").exists():
            shutil.copy2(d / "seed.iso", stage / "seed.iso")
        (stage / "meta.json").write_text(json.dumps({"name": name, "type": "kvm", "created": time.time()}))
        job.write("Packe Backup-Archiv …")
        with tarfile.open(target, "w") as tar:
            for f in stage.iterdir():
                tar.add(f, arcname=f.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"file": target.name, "size": target.stat().st_size}


def restore(job, name: str, archive: Path) -> dict:
    d = _guest_dir(name)
    stage = BACKUP_DIR / f".restore-{name}-{int(time.time())}"
    stage.mkdir(parents=True)
    try:
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                if member.name not in ("disk.qcow2", "domain.xml", "seed.iso", "meta.json") or not member.isfile():
                    raise CmdError(f"Unerwartete Datei im Backup: {member.name}")
            tar.extractall(stage)
        if exists(name):
            job.write("Stoppe und entferne aktuelle VM-Definition …")
            run(["virsh", "destroy", name], check=False)
            run(["virsh", "undefine", name, "--snapshots-metadata", "--managed-save"], job=job)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o755)
        job.write("Stelle Festplatte wieder her …")
        shutil.move(str(stage / "disk.qcow2"), d / "disk.qcow2")
        if (stage / "seed.iso").exists():
            shutil.move(str(stage / "seed.iso"), d / "seed.iso")
        shutil.move(str(stage / "domain.xml"), d / "domain.xml")
        run(["virsh", "define", d / "domain.xml"], job=job)
        run(["virsh", "autostart", name], check=False)
        run(["virsh", "start", name], job=job)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"state": state(name)}
