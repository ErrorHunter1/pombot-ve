"""Cloud-Images und ISOs herunterladen und zwischenspeichern."""
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from .config import IMAGE_DIR, ISO_DIR
from .util import CmdError, run

_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)
_ID_RE = re.compile(r"^[0-9a-f]{16}\.(img|iso)$")


def _cache_path(url: str, directory: Path, suffix: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return directory / f"{digest}{suffix}"


def _download(url: str, dest: Path, job=None) -> None:
    if not url.startswith(("https://", "http://")):
        raise CmdError("Nur http(s)-URLs sind erlaubt")
    tmp = dest.with_name(dest.name + ".part")
    if job:
        job.write(f"Lade herunter: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "pombot-agent"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, -10
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if job and total:
                    pct = done * 100 // total
                    if pct >= last + 10:
                        last = pct - pct % 10
                        job.write(f"Download {pct}% ({done >> 20} / {total >> 20} MiB)")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise CmdError(f"Download fehlgeschlagen: {exc}")
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    dest.with_name(dest.name + ".json").write_text(json.dumps({"url": url, "downloaded": time.time()}))


def _ensure(url: str, directory: Path, suffix: str, job=None) -> Path:
    dest = _cache_path(url, directory, suffix)
    with _LOCKS[str(dest)]:
        if dest.exists():
            if job:
                job.write(f"Verwende zwischengespeichertes Image {dest.name}")
            return dest
        _download(url, dest, job)
        return dest


def ensure_cloud_image(url: str, job=None) -> Path:
    return _ensure(url, IMAGE_DIR, ".img", job)


def ensure_iso(url: str, job=None) -> Path:
    return _ensure(url, ISO_DIR, ".iso", job)


def image_info(path: Path) -> dict:
    return json.loads(run(["qemu-img", "info", "--output=json", path]))


def list_images() -> list[dict]:
    result = []
    for directory, kind in ((IMAGE_DIR, "cloud"), (ISO_DIR, "iso")):
        for f in sorted(directory.glob("*.img")) + sorted(directory.glob("*.iso")):
            meta = {}
            meta_file = f.with_name(f.name + ".json")
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                except ValueError:
                    pass
            result.append({
                "id": f.name,
                "kind": kind,
                "url": meta.get("url"),
                "downloaded": meta.get("downloaded"),
                "size": f.stat().st_size,
            })
    return result


def delete_image(image_id: str) -> None:
    if not _ID_RE.match(image_id):
        raise CmdError("Ungültige Image-ID")
    directory = IMAGE_DIR if image_id.endswith(".img") else ISO_DIR
    path = directory / image_id
    if not path.exists():
        raise CmdError("Image nicht gefunden")
    path.unlink()
    path.with_name(path.name + ".json").unlink(missing_ok=True)


def pull(job, url: str, kind: str) -> dict:
    path = ensure_iso(url, job) if kind == "iso" else ensure_cloud_image(url, job)
    return {"id": path.name}
