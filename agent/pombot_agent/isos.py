"""ISO-Bibliothek des Nodes: hochladen (in Stücken), von URL laden, auflisten, löschen."""
import json
import os
import re
import time
import uuid
from pathlib import Path

from .config import ISO_DIR
from .images import _download
from .util import CmdError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,150}\.iso$")
UPLOAD_RE = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_DIR = ISO_DIR / ".uploads"
MAX_SIZE = 64 << 30  # 64 GB


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name.lower().endswith(".iso"):
        name += ".iso"
    if not NAME_RE.match(name):
        raise CmdError("Ungültiger Dateiname – erlaubt: Buchstaben, Ziffern, . _ + - und Endung .iso")
    return name


def iso_path(name: str) -> Path:
    path = ISO_DIR / check_name(name)
    if not path.exists():
        raise CmdError(f"ISO {name} nicht gefunden")
    return path


def _meta(path: Path) -> dict:
    meta_file = path.with_name(path.name + ".json")
    try:
        return json.loads(meta_file.read_text()) if meta_file.exists() else {}
    except ValueError:
        return {}


def list_isos() -> list[dict]:
    result = []
    for f in sorted(ISO_DIR.glob("*.iso"), key=lambda p: p.name.lower()):
        meta = _meta(f)
        st = f.stat()
        result.append({"file": f.name, "name": meta.get("name") or f.name, "size": st.st_size,
                       "url": meta.get("url"), "added": meta.get("downloaded") or meta.get("uploaded") or st.st_mtime,
                       "template": bool(re.match(r"^[0-9a-f]{16}\.iso$", f.name))})
    return result


def delete(name: str) -> None:
    path = iso_path(name)
    path.unlink()
    path.with_name(path.name + ".json").unlink(missing_ok=True)


def _write_meta(path: Path, **data) -> None:
    path.with_name(path.name + ".json").write_text(json.dumps({"name": path.name, **data}))


def _looks_like_iso(path: Path) -> bool:
    """ISO-9660 hat bei Byte 32769 die Kennung CD001 (UDF-Images oft ebenfalls)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(32769)
            return fh.read(5) in (b"CD001", b"BEA01", b"NSR02")
    except OSError:
        return False


# ---------------------------------------------------------------- Upload in Stücken

def upload_start() -> str:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # alte, abgebrochene Uploads (älter als 1 Tag) aufräumen
    for f in UPLOAD_DIR.glob("*.part"):
        if time.time() - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)
    upload_id = uuid.uuid4().hex
    (UPLOAD_DIR / f"{upload_id}.part").touch()
    return upload_id


def _part(upload_id: str) -> Path:
    if not UPLOAD_RE.match(upload_id or ""):
        raise CmdError("Ungültige Upload-ID")
    path = UPLOAD_DIR / f"{upload_id}.part"
    if not path.exists():
        raise CmdError("Upload nicht gefunden oder abgelaufen")
    return path


def upload_offset(upload_id: str) -> int:
    return _part(upload_id).stat().st_size


def open_chunk(upload_id: str, offset: int):
    """Öffnet die Teildatei zum Anhängen – nur wenn `offset` genau dem bisherigen Stand entspricht."""
    path = _part(upload_id)
    size = path.stat().st_size
    if offset != size:
        raise CmdError(f"Falscher Offset {offset}, bisher empfangen: {size}")
    if size > MAX_SIZE:
        raise CmdError("Datei zu groß")
    return open(path, "ab")


def upload_finish(upload_id: str, name: str, expected_size: int | None = None) -> dict:
    path = _part(upload_id)
    name = check_name(name)
    target = ISO_DIR / name
    if target.exists():
        raise CmdError(f"{name} existiert bereits – bitte anderen Namen wählen oder die alte ISO löschen")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise CmdError(f"Upload unvollständig: {path.stat().st_size} von {expected_size} Bytes")
    if not _looks_like_iso(path):
        path.unlink(missing_ok=True)
        raise CmdError("Die Datei ist kein gültiges ISO-Abbild")
    os.chmod(path, 0o644)
    os.replace(path, target)
    _write_meta(target, uploaded=time.time())
    return {"file": name, "size": target.stat().st_size}


def upload_abort(upload_id: str) -> None:
    try:
        _part(upload_id).unlink(missing_ok=True)
    except CmdError:
        pass


# ---------------------------------------------------------------- Von URL laden

def fetch(job, url: str, name: str) -> dict:
    name = check_name(name)
    target = ISO_DIR / name
    if target.exists():
        raise CmdError(f"{name} existiert bereits")
    _download(url, target, job)
    if not _looks_like_iso(target):
        target.unlink(missing_ok=True)
        target.with_name(target.name + ".json").unlink(missing_ok=True)
        raise CmdError("Die heruntergeladene Datei ist kein gültiges ISO-Abbild")
    _write_meta(target, url=url, downloaded=time.time())
    job.write(f"ISO gespeichert: {name} ({target.stat().st_size >> 20} MiB)")
    return {"file": name}
