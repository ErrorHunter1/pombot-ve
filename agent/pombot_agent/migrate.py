"""Migration zwischen Nodes: Server als tar-Datenstrom exportieren bzw. importieren.

Der Datenstrom läuft über das Panel (Quelle → Panel → Ziel). Die Nodes müssen sich deshalb nicht
gegenseitig erreichen können. Der Server muss während Export/Import gestoppt sein.

Aufbau des Archivs:
  manifest.json            {"name", "type", "agent_version"}
  kvm/<name>/...           Inhalt von /var/lib/pombot/guests/<name> (disk.qcow2, seed.iso, domain.xml …)
  lxc/<name>/...           Inhalt von /var/lib/lxc/<name>
  extra/<name>/...         Inhalt von /var/lib/pombot/guests/<name> bei Containern (Snapshots)
"""
import asyncio
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import kvm, lxc
from .config import GUEST_DIR, ISO_DIR, LXC_PATH, VERSION
from .util import CmdError, check_name, run

CHUNK = 1 << 20


def _sources(name: str) -> tuple[str, list[tuple[Path, str]], int]:
    """Welche Verzeichnisse exportiert werden und wie groß sie ungefähr sind."""
    check_name(name)
    if lxc.exists(name):
        if lxc.state(name) != "stopped":
            raise CmdError("Der Container muss gestoppt sein")
        dirs = [(LXC_PATH / name, f"lxc/{name}")]
        if (GUEST_DIR / name).exists():
            dirs.append((GUEST_DIR / name, f"extra/{name}"))
        gtype = "lxc"
    elif kvm.exists(name):
        if kvm.state(name) != "stopped":
            raise CmdError("Die VM muss gestoppt sein")
        (GUEST_DIR / name / "domain.xml").write_text(run(["virsh", "dumpxml", "--inactive", name]))
        dirs = [(GUEST_DIR / name, f"kvm/{name}")]
        gtype = "kvm"
    else:
        raise CmdError("Server nicht gefunden")
    size = 0
    for d, _ in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    size += os.lstat(os.path.join(root, f)).st_blocks * 512
                except OSError:
                    pass
    return gtype, dirs, size


def export_info(name: str) -> dict:
    gtype, _, size = _sources(name)
    snaps = kvm.snapshots(name) if gtype == "kvm" else []
    return {"type": gtype, "size": size, "kvm_snapshots": len(snaps)}


async def _run_tar(args: list[str]):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                break
            yield chunk
        rc = await proc.wait()
        if rc not in (0, 1):  # 1 = "Datei hat sich beim Lesen geändert" – bei gestopptem Server unkritisch
            err = (await proc.stderr.read()).decode(errors="replace")[-500:]
            raise CmdError(f"Export fehlgeschlagen: {err}")
    finally:
        if proc.returncode is None:
            proc.kill()


async def export_stream(name: str):
    """Liefert den Datenstrom: mehrere tar-Archive hintereinander (Import liest sie mit tar -i)."""
    gtype, dirs, _ = _sources(name)
    stage = Path(tempfile.mkdtemp(prefix="pombot-export-"))
    try:
        (stage / "manifest.json").write_text(json.dumps({"name": name, "type": gtype, "agent_version": VERSION}))
        async for chunk in _run_tar(["tar", "-cf", "-", "-C", str(stage), "manifest.json"]):
            yield chunk
        for src, arc in dirs:
            # jedes Verzeichnis als eigenes Archiv, damit die Umbenennung eindeutig bleibt
            args = ["tar", "--numeric-owner", "--sparse", "-cf", "-",
                    "--transform", f"s,^{src.name}(/|$),{arc}\\1,x", "-C", str(src.parent), src.name]
            async for chunk in _run_tar(args):
                yield chunk
    finally:
        shutil.rmtree(stage, ignore_errors=True)


async def import_stream(name: str, body_iter) -> dict:
    """Nimmt den Datenstrom entgegen, entpackt ihn und legt den Server auf diesem Node an (gestoppt)."""
    check_name(name)
    if lxc.exists(name) or kvm.exists(name) or (GUEST_DIR / name).exists() or (LXC_PATH / name).exists():
        raise CmdError(f"{name} existiert auf diesem Node bereits")
    stage = Path(tempfile.mkdtemp(prefix="pombot-import-", dir=str(GUEST_DIR.parent)))
    proc = await asyncio.create_subprocess_exec("tar", "--numeric-owner", "-xpif", "-", "-C", str(stage),
                                                stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    received = 0
    try:
        async for chunk in body_iter:
            if chunk:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
                received += len(chunk)
        proc.stdin.close()
        rc = await proc.wait()
        if rc != 0:
            raise CmdError("Entpacken fehlgeschlagen: " + (await proc.stderr.read()).decode(errors="replace")[-500:])
        manifest = json.loads((stage / "manifest.json").read_text())
        if manifest.get("name") != name:
            raise CmdError("Archiv gehört zu einem anderen Server")
        await asyncio.to_thread(_install, stage, name, manifest["type"])
        return {"type": manifest["type"], "received": received}
    except Exception:
        if proc.returncode is None:
            proc.kill()
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _install(stage: Path, name: str, gtype: str) -> None:
    if gtype == "kvm":
        src = stage / "kvm" / name
        dst = GUEST_DIR / name
        shutil.move(str(src), dst)
        os.chmod(dst, 0o755)
        root = ET.fromstring((dst / "domain.xml").read_text())
        # ISOs aus der Bibliothek des alten Nodes gibt es hier evtl. nicht – Laufwerk dann leeren
        for disk in root.findall("./devices/disk[@device='cdrom']"):
            source = disk.find("source")
            if source is not None and not Path(source.get("file", "")).exists():
                if Path(source.get("file", "")).parent == ISO_DIR:
                    disk.remove(source)
        (dst / "domain.xml").write_text(ET.tostring(root, encoding="unicode"))
        try:
            run(["virsh", "define", dst / "domain.xml"])
            run(["virsh", "autostart", name], check=False)
        except CmdError:
            shutil.rmtree(dst, ignore_errors=True)
            raise
    elif gtype == "lxc":
        shutil.move(str(stage / "lxc" / name), LXC_PATH / name)
        if (stage / "extra" / name).exists():
            shutil.move(str(stage / "extra" / name), GUEST_DIR / name)
    else:
        raise CmdError("Unbekannter Servertyp im Archiv")
