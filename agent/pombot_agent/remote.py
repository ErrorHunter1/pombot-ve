"""Externe Backup-Speicher: SFTP und S3 über rclone, NFS und SMB über Mounts.

Die Zugangsdaten schickt das Panel bei jedem Aufruf mit (über die TLS-gesicherte Verbindung); der Agent
speichert sie nicht dauerhaft. rclone bekommt sie über Umgebungsvariablen, SMB über eine temporäre
Credentials-Datei – so tauchen Passwörter nicht in der Prozessliste auf.
"""
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .config import BACKUP_DIR
from .util import CmdError, run

MOUNT_BASE = Path("/mnt/pombot-backup")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
PATH_RE = re.compile(r"^[A-Za-z0-9._\-/ ]{0,300}$")
NAME_RE = re.compile(r"^pv\d+-(kvm|lxc)-\d{8}-\d{6}(-auto)?\.tar(\.gz)?$")
TYPES = ("sftp", "s3", "nfs", "smb")


def _clean_path(p: str | None) -> str:
    p = (p or "").strip().strip("/")
    if not PATH_RE.match(p) or ".." in p.split("/"):
        raise CmdError("Ungültiger Pfad")
    return p


def _check(t: dict) -> dict:
    if t.get("type") not in TYPES:
        raise CmdError("Unbekannter Speichertyp")
    t = dict(t)
    t["path"] = _clean_path(t.get("path"))
    if t["type"] in ("sftp",):
        if not HOST_RE.match(str(t.get("host", ""))):
            raise CmdError("Ungültiger Host")
        if not re.match(r"^[A-Za-z0-9._\-@]{1,64}$", str(t.get("user", ""))):
            raise CmdError("Ungültiger Benutzername")
    if t["type"] == "s3":
        if not re.match(r"^[a-z0-9.\-]{3,63}$", str(t.get("bucket", ""))):
            raise CmdError("Ungültiger Bucket-Name")
        if t.get("endpoint") and not re.match(r"^https?://[A-Za-z0-9.\-:/]+$", t["endpoint"]):
            raise CmdError("Ungültiger Endpoint")
    if t["type"] == "nfs":
        if not HOST_RE.match(str(t.get("server", ""))) or not str(t.get("export", "")).startswith("/"):
            raise CmdError("NFS: Server und Export (z. B. /backups) angeben")
    if t["type"] == "smb":
        if not re.match(r"^//[A-Za-z0-9.\-]+/[^\s/][^\s]*$", str(t.get("share", ""))):
            raise CmdError("SMB: Freigabe als //server/freigabe angeben")
    if not re.match(r"^[A-Za-z0-9_\-]{1,40}$", str(t.get("id", "x"))):
        raise CmdError("Ungültige Ziel-ID")
    return t


def _check_sub(sub: str) -> str:
    if not re.match(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$", sub or ""):
        raise CmdError("Ungültiger Unterordner")
    return sub


# ---------------------------------------------------------------- rclone (SFTP, S3)

def _obscure(secret: str) -> str:
    try:  # rclone >= 1.54 liest das Passwort von der Standardeingabe (taucht nicht in der Prozessliste auf)
        return run(["rclone", "obscure", "-"], input_text=secret, log=False, timeout=30).strip()
    except CmdError:  # ältere Versionen (z. B. Ubuntu 22.04) nur als Argument
        return run(["rclone", "obscure", secret], log=False, timeout=30).strip()


def _rclone(t: dict) -> tuple[dict, str]:
    env = {"RCLONE_CONFIG_PBT_TYPE": t["type"]}
    if t["type"] == "sftp":
        env.update({"RCLONE_CONFIG_PBT_HOST": t["host"], "RCLONE_CONFIG_PBT_USER": t["user"],
                    "RCLONE_CONFIG_PBT_PORT": str(int(t.get("port") or 22)),
                    "RCLONE_CONFIG_PBT_SHELL_TYPE": "unix", "RCLONE_CONFIG_PBT_MD5SUM_COMMAND": "none",
                    "RCLONE_CONFIG_PBT_SHA1SUM_COMMAND": "none"})
        if t.get("password"):
            env["RCLONE_CONFIG_PBT_PASS"] = _obscure(t["password"])
        if t.get("private_key"):
            env["RCLONE_CONFIG_PBT_KEY_PEM"] = t["private_key"].strip() + "\n"
        root = f"pbt:{t['path']}" if t["path"] else "pbt:"
    else:
        env.update({"RCLONE_CONFIG_PBT_PROVIDER": t.get("provider") or "Other",
                    "RCLONE_CONFIG_PBT_ACCESS_KEY_ID": t.get("access_key", ""),
                    "RCLONE_CONFIG_PBT_SECRET_ACCESS_KEY": t.get("secret_key", ""),
                    "RCLONE_CONFIG_PBT_NO_CHECK_BUCKET": "true"})
        if t.get("endpoint"):
            env["RCLONE_CONFIG_PBT_ENDPOINT"] = t["endpoint"]
        if t.get("region"):
            env["RCLONE_CONFIG_PBT_REGION"] = t["region"]
        root = f"pbt:{t['bucket']}" + (f"/{t['path']}" if t["path"] else "")
    env["RCLONE_CONFIG"] = "/dev/null"  # keine fremde Konfiguration einlesen
    return env, root


def _rc(t: dict, args: list, job=None, timeout=6 * 3600) -> str:
    env, _ = _rclone(t)
    return run(["rclone", "--contimeout", "20s", "--timeout", "5m", "--retries", "3", "--low-level-retries", "5",
                *args], env=env, job=job, timeout=timeout)


# ---------------------------------------------------------------- Mounts (NFS, SMB)

def _mount(t: dict) -> Path:
    mp = MOUNT_BASE / str(t["id"])
    mp.mkdir(parents=True, exist_ok=True)
    if os.path.ismount(mp):
        return mp
    if t["type"] == "nfs":
        opts = t.get("options") or "vers=4,soft,timeo=150,retrans=3"
        if not re.match(r"^[A-Za-z0-9=,._\-]{1,200}$", opts):
            raise CmdError("Ungültige NFS-Optionen")
        run(["mount", "-t", "nfs", "-o", opts, f"{t['server']}:{t['export']}", mp], timeout=60)
    else:
        with tempfile.NamedTemporaryFile("w", delete=False, prefix="pombot-smb-") as fh:
            fh.write(f"username={t.get('user', '')}\npassword={t.get('password', '')}\n")
            if t.get("domain"):
                fh.write(f"domain={t['domain']}\n")
            cred = fh.name
        os.chmod(cred, 0o600)
        try:
            opts = f"credentials={cred},vers={t.get('version') or '3.0'},uid=0,gid=0,file_mode=0600,dir_mode=0700"
            run(["mount", "-t", "cifs", "-o", opts, t["share"], mp], timeout=60)
        finally:
            os.unlink(cred)
    return mp


def _mount_dir(t: dict, sub: str = "") -> Path:
    base = _mount(t)
    d = base / t["path"] if t["path"] else base
    return d / sub if sub else d


# ---------------------------------------------------------------- Öffentliche Funktionen

def test(t: dict) -> dict:
    """Schreibt, liest und löscht eine kleine Testdatei."""
    t = _check(t)
    name = f".pombot-test-{int(time.time())}"
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write("pombot")
        local = fh.name
    try:
        if t["type"] in ("sftp", "s3"):
            _, root = _rclone(t)
            _rc(t, ["copyto", local, f"{root}/{name}"], timeout=120)
            _rc(t, ["deletefile", f"{root}/{name}"], timeout=120)
            total = None
        else:
            d = _mount_dir(t)
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, d / name)
            (d / name).unlink()
            total = shutil.disk_usage(d).free
    finally:
        os.unlink(local)
    return {"ok": True, "free": total}


def upload(job, t: dict, file: str, sub: str, delete_local: bool = False) -> dict:
    t, sub = _check(t), _check_sub(sub)
    if not NAME_RE.match(file):
        raise CmdError("Ungültiger Backup-Name")
    src = BACKUP_DIR / file
    if not src.exists():
        raise CmdError("Backup nicht gefunden")
    size = src.stat().st_size
    job.write(f"Übertrage {file} ({size >> 20} MiB) zu {t.get('name') or t['type']} …")
    started = time.time()
    if t["type"] in ("sftp", "s3"):
        _, root = _rclone(t)
        _rc(t, ["copyto", "--stats", "30s", "--stats-one-line", "-v", src, f"{root}/{sub}/{file}"], job=job)
    else:
        d = _mount_dir(t, sub)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (file + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, d / file)
    secs = max(time.time() - started, 1)
    job.write(f"Hochgeladen in {int(secs)} s ({size / secs / (1 << 20):.1f} MiB/s).")
    if delete_local:
        src.unlink()
        job.write("Lokale Kopie gelöscht.")
    return {"file": file, "size": size}


def list_files(t: dict, sub: str) -> list[dict]:
    t, sub = _check(t), _check_sub(sub)
    result = []
    if t["type"] in ("sftp", "s3"):
        _, root = _rclone(t)
        try:
            out = _rc(t, ["lsjson", "--files-only", f"{root}/{sub}"], timeout=120)
        except CmdError as exc:
            if "not found" in str(exc).lower() or "directory not found" in str(exc).lower():
                return []
            raise
        for entry in json.loads(out or "[]"):
            if NAME_RE.match(entry["Name"]):
                ts = entry.get("ModTime", "")
                try:
                    from datetime import datetime
                    created = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    created = 0
                result.append({"file": entry["Name"], "size": entry.get("Size", 0), "created": created})
    else:
        d = _mount_dir(t, sub)
        if d.exists():
            for f in d.iterdir():
                if NAME_RE.match(f.name):
                    st = f.stat()
                    result.append({"file": f.name, "size": st.st_size, "created": st.st_mtime})
    for r in result:
        r["auto"] = "-auto." in r["file"]
    return sorted(result, key=lambda r: r["created"], reverse=True)


def fetch(job, t: dict, sub: str, file: str) -> dict:
    """Lädt ein externes Backup in den lokalen Backup-Ordner (z. B. zum Wiederherstellen)."""
    t, sub = _check(t), _check_sub(sub)
    if not NAME_RE.match(file):
        raise CmdError("Ungültiger Backup-Name")
    dst = BACKUP_DIR / file
    job.write(f"Lade {file} von {t.get('name') or t['type']} …")
    if t["type"] in ("sftp", "s3"):
        _, root = _rclone(t)
        _rc(t, ["copyto", "--stats", "30s", "--stats-one-line", "-v", f"{root}/{sub}/{file}", dst], job=job)
    else:
        shutil.copyfile(_mount_dir(t, sub) / file, dst)
    return {"file": file, "size": dst.stat().st_size}


def delete(t: dict, sub: str, file: str) -> None:
    t, sub = _check(t), _check_sub(sub)
    if not NAME_RE.match(file):
        raise CmdError("Ungültiger Backup-Name")
    if t["type"] in ("sftp", "s3"):
        _, root = _rclone(t)
        _rc(t, ["deletefile", f"{root}/{sub}/{file}"], timeout=300)
    else:
        (_mount_dir(t, sub) / file).unlink(missing_ok=True)
