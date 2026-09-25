"""Gemeinsamer Speicher (NFS/SMB) für Server-Festplatten, HA und schnelle Migration.

Jeder Speicher wird auf allen Nodes unter /mnt/pombot-storage/<id> eingehängt. Ein Server auf diesem
Speicher hat sein Verzeichnis unter <mount>/guests/<name>; /var/lib/pombot/guests/<name> ist dann nur ein
Symlink dorthin – der übrige Agent-Code funktioniert dadurch unverändert.
Das Panel schickt die Liste der Speicher regelmäßig (PUT /storage); der Agent merkt sie sich in
/var/lib/pombot/storage.json und hängt sie nach einem Neustart selbst wieder ein.
"""
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from .config import DATA_DIR, GUEST_DIR
from .util import CmdError, check_name, run

BASE = Path("/mnt/pombot-storage")
STATE = DATA_DIR / "storage.json"
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
_LOCK = threading.Lock()


def _validate(s: dict) -> dict:
    s = dict(s)
    if not re.match(r"^[0-9]{1,9}$", str(s.get("id", ""))):
        raise CmdError("Ungültige Speicher-ID")
    s["id"] = str(s["id"])
    if s.get("type") == "nfs":
        if not HOST_RE.match(str(s.get("server", ""))) or not str(s.get("export", "")).startswith("/"):
            raise CmdError("NFS: Server und Export angeben")
        opts = s.get("options") or "vers=4,hard,timeo=600,retrans=5"
        if not re.match(r"^[A-Za-z0-9=,._\-]{1,200}$", opts):
            raise CmdError("Ungültige NFS-Optionen")
        s["options"] = opts
    elif s.get("type") == "smb":
        if not re.match(r"^//[A-Za-z0-9.\-]+/[^\s/][^\s]*$", str(s.get("share", ""))):
            raise CmdError("SMB-Freigabe als //server/freigabe angeben")
    elif s.get("type") == "path":
        p = str(s.get("path", ""))
        if not p.startswith("/") or ".." in p.split("/"):
            raise CmdError("Pfad muss absolut sein, z. B. /mnt/ceph")
    else:
        raise CmdError("Unbekannter Speichertyp")
    return s


def mountpoint(s: dict) -> Path:
    return Path(s["path"]) if s["type"] == "path" else BASE / str(s["id"])


def mount(s: dict) -> Path:
    s = _validate(s)
    mp = mountpoint(s)
    if s["type"] == "path":
        if not mp.is_dir():
            raise CmdError(f"{mp} existiert nicht")
        return mp
    mp.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(mp):
        return mp
    if s["type"] == "nfs":
        run(["mount", "-t", "nfs", "-o", s["options"], f"{s['server']}:{s['export']}", mp], timeout=60)
    else:
        with tempfile.NamedTemporaryFile("w", delete=False, prefix="pombot-smb-") as fh:
            fh.write(f"username={s.get('user', '')}\npassword={s.get('password', '')}\n")
            if s.get("domain"):
                fh.write(f"domain={s['domain']}\n")
            cred = fh.name
        os.chmod(cred, 0o600)
        try:
            opts = f"credentials={cred},vers={s.get('version') or '3.0'},uid=0,gid=0,file_mode=0660,dir_mode=0775,nobrl"
            run(["mount", "-t", "cifs", "-o", opts, s["share"], mp], timeout=60)
        finally:
            os.unlink(cred)
    return mp


def status(s: dict) -> dict:
    s = _validate(s)
    mp = mountpoint(s)
    mounted = mp.is_dir() and (s["type"] == "path" or os.path.ismount(mp))
    info = {"id": s["id"], "mounted": mounted, "path": str(mp)}
    if mounted:
        du = shutil.disk_usage(mp)
        info.update(total=du.total, used=du.used, free=du.free)
    return info


def sync(storages: list[dict]) -> list[dict]:
    """Übernimmt die Speicherliste vom Panel, hängt alles ein und meldet den Zustand zurück."""
    with _LOCK:
        clean = [_validate(s) for s in storages]
        STATE.write_text(json.dumps(clean))
        STATE.chmod(0o600)
        result = []
        for s in clean:
            try:
                mount(s)
                result.append(status(s))
            except CmdError as exc:
                result.append({"id": s["id"], "mounted": False, "error": str(exc)})
        return result


def ensure_mounted() -> None:
    """Nach Neustart/Verbindungsabbruch: bekannte Speicher erneut einhängen (Watchdog)."""
    if not STATE.exists():
        return
    try:
        storages = json.loads(STATE.read_text())
    except ValueError:
        return
    for s in storages:
        try:
            mount(s)
        except CmdError:
            pass


def known(storage_id: str) -> dict:
    if not STATE.exists():
        raise CmdError("Speicher auf diesem Node unbekannt")
    for s in json.loads(STATE.read_text()):
        if str(s["id"]) == str(storage_id):
            return s
    raise CmdError("Speicher auf diesem Node unbekannt")


def guest_dir_on(storage_id: str, name: str) -> Path:
    s = known(storage_id)
    mp = mount(s)
    return mp / "guests" / check_name(name)


def prepare_guest_dir(storage_id: str | None, name: str) -> Path:
    """Legt das Server-Verzeichnis an – lokal oder auf dem Speicher (mit Symlink unter GUEST_DIR)."""
    local = GUEST_DIR / check_name(name)
    if not storage_id:
        local.mkdir(parents=True, exist_ok=True)
        return local
    target = guest_dir_on(storage_id, name)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o755)
    if local.is_symlink() or local.exists():
        if local.is_symlink() and local.resolve() == target.resolve():
            return target
        raise CmdError(f"{local} existiert bereits")
    local.symlink_to(target)
    return target


def adopt_link(storage_id: str, name: str) -> Path:
    """Auf einem anderen Node: Symlink auf das vorhandene Server-Verzeichnis im Speicher anlegen."""
    target = guest_dir_on(storage_id, name)
    if not target.exists():
        raise CmdError("Server-Verzeichnis auf dem Speicher nicht gefunden")
    local = GUEST_DIR / name
    if local.is_symlink():
        local.unlink()
    elif local.exists():
        raise CmdError(f"{local} existiert lokal – Konflikt")
    local.symlink_to(target)
    return target


def remove_guest_dirs(name: str) -> None:
    """Entfernt das Server-Verzeichnis – bei Speicher-Servern samt Daten auf dem Speicher und Symlinks."""
    from .config import LXC_PATH
    local = GUEST_DIR / check_name(name)
    if local.is_symlink():
        target = local.resolve()
        local.unlink()
        shutil.rmtree(target, ignore_errors=True)
    else:
        shutil.rmtree(local, ignore_errors=True)
    lxc_link = LXC_PATH / name
    if lxc_link.is_symlink():
        lxc_link.unlink()


def guest_storage(name: str) -> str | None:
    """ID des Speichers, auf dem der Server liegt (None = lokal)."""
    local = GUEST_DIR / check_name(name)
    if not local.is_symlink():
        return None
    real = local.resolve()
    if STATE.exists():
        for s in json.loads(STATE.read_text()):
            try:
                if real.is_relative_to(mountpoint(s)):
                    return str(s["id"])
            except (OSError, ValueError):
                continue
    return "unknown"
