"""Externe Backup-Speicher: Verwaltung (Admin) und Hilfsfunktionen für Upload/Aufräumen."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .agent_client import AgentClient, AgentError
from .db import get_db
from .models import BackupTarget, Guest, Node, User
from .security import audit, client_ip, require_admin

FIELDS = {
    "sftp": ["host", "port", "user", "password", "private_key", "path"],
    "s3": ["provider", "endpoint", "region", "bucket", "access_key", "secret_key", "path"],
    "nfs": ["server", "export", "options", "path"],
    "smb": ["share", "user", "password", "domain", "version", "path"],
}
SECRETS = {"password", "private_key", "secret_key"}
LABELS = {"sftp": "SFTP", "s3": "S3", "nfs": "NFS", "smb": "SMB"}

router = APIRouter(prefix="/api/admin/backup-targets", tags=["backup-targets"])


def payload(t: BackupTarget) -> dict:
    """Konfiguration, wie der Agent sie erwartet (inkl. Zugangsdaten)."""
    return {"id": str(t.id), "name": t.name, "type": t.type, **t.config}


def public(t: BackupTarget) -> dict:
    cfg = t.config
    shown = {k: ("" if k in SECRETS else v) for k, v in cfg.items()}
    for k in SECRETS:
        if k in FIELDS[t.type]:
            shown[f"{k}_set"] = bool(cfg.get(k))
    where = {"sftp": f"{cfg.get('user')}@{cfg.get('host')}:{cfg.get('path') or '~'}",
             "s3": f"{cfg.get('bucket')}/{cfg.get('path') or ''} ({cfg.get('endpoint') or 'AWS'})",
             "nfs": f"{cfg.get('server')}:{cfg.get('export')}/{cfg.get('path') or ''}",
             "smb": f"{cfg.get('share')}/{cfg.get('path') or ''}"}[t.type]
    return {"id": t.id, "name": t.name, "type": t.type, "type_label": LABELS[t.type], "where": where,
            "user_visible": t.user_visible, "config": shown}


def subdir(g: Guest) -> str:
    """Ordner eines Servers auf dem Speicher – eindeutig je Panel-Installation und VMID."""
    from .heartbeat import installation_id
    return f"pombot-{installation_id()[:8]}/{g.agent_name}"


def visible(db: Session, user: User) -> list[BackupTarget]:
    q = select(BackupTarget).order_by(BackupTarget.name)
    return [t for t in db.scalars(q) if user.is_admin or t.user_visible]


def get_visible(db: Session, user: User, target_id: int | None) -> BackupTarget | None:
    if target_id in (None, 0, ""):
        return None
    t = db.get(BackupTarget, int(target_id))
    if not t or (not user.is_admin and not t.user_visible):
        raise HTTPException(404, "Backup-Speicher nicht gefunden")
    return t


# ---------------------------------------------------------------- Admin-API

class TargetBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern="^(sftp|s3|nfs|smb)$")
    config: dict
    user_visible: bool = False


def _clean(body: TargetBody, old: dict | None = None) -> dict:
    old = old or {}
    cfg = {}
    for key in FIELDS[body.type]:
        value = body.config.get(key)
        if key in SECRETS and (value is None or value == ""):
            value = old.get(key, "")  # leer = bisherigen Wert behalten
        if value is None:
            value = ""
        if key == "port":
            try:
                value = int(value or 22)
            except (TypeError, ValueError):
                raise HTTPException(400, "Port muss eine Zahl sein")
        cfg[key] = value.strip() if isinstance(value, str) and key not in ("private_key",) else value
    cfg["path"] = str(cfg.get("path") or "").strip().strip("/")
    if ".." in cfg["path"].split("/"):
        raise HTTPException(400, "Ungültiger Pfad")
    required = {"sftp": ["host", "user"], "s3": ["bucket", "access_key", "secret_key"], "nfs": ["server", "export"],
                "smb": ["share"]}[body.type]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise HTTPException(400, f"Bitte ausfüllen: {', '.join(missing)}")
    if body.type == "sftp" and not (cfg.get("password") or cfg.get("private_key")):
        raise HTTPException(400, "SFTP: Passwort oder privaten Schlüssel angeben")
    if body.type == "smb" and not re.match(r"^//[^/\s]+/\S+$", cfg["share"]):
        raise HTTPException(400, "SMB-Freigabe als //server/freigabe angeben")
    if body.type == "nfs" and not cfg["export"].startswith("/"):
        raise HTTPException(400, "NFS-Export beginnt mit /, z. B. /srv/backups")
    return cfg


@router.get("")
def list_targets(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [public(t) for t in db.scalars(select(BackupTarget).order_by(BackupTarget.name))]


@router.post("")
def create_target(body: TargetBody, request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    if db.scalar(select(BackupTarget).where(BackupTarget.name == body.name)):
        raise HTTPException(400, "Name bereits vergeben")
    t = BackupTarget(name=body.name.strip(), type=body.type, config_json=json.dumps(_clean(body)),
                     user_visible=body.user_visible)
    db.add(t)
    audit(db, user, "backup-target-create", f"{t.name} ({t.type})", client_ip(request))
    db.commit()
    return public(t)


@router.put("/{target_id}")
def update_target(target_id: int, body: TargetBody, request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    t = db.get(BackupTarget, target_id)
    if not t:
        raise HTTPException(404, "Backup-Speicher nicht gefunden")
    t.config_json = json.dumps(_clean(body, t.config if body.type == t.type else {}))
    t.name, t.type, t.user_visible = body.name.strip(), body.type, body.user_visible
    audit(db, user, "backup-target-update", t.name, client_ip(request))
    db.commit()
    return public(t)


@router.delete("/{target_id}")
def delete_target(target_id: int, request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    t = db.get(BackupTarget, target_id)
    if not t:
        raise HTTPException(404, "Backup-Speicher nicht gefunden")
    audit(db, user, "backup-target-delete", t.name, client_ip(request))
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.post("/{target_id}/test")
def test_target(target_id: int, node_id: int | None = None, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    """Prüft vom gewählten (oder ersten erreichbaren) Node aus: schreiben, lesen, löschen."""
    t = db.get(BackupTarget, target_id)
    if not t:
        raise HTTPException(404, "Backup-Speicher nicht gefunden")
    nodes = [db.get(Node, node_id)] if node_id else list(db.scalars(select(Node).where(Node.status == "online")))
    if not nodes or not nodes[0]:
        raise HTTPException(400, "Kein Node online, von dem aus getestet werden kann")
    results = []
    for node in nodes:
        try:
            r = AgentClient.for_node(node).request("POST", "/remote/test", {"target": payload(t)}, timeout=180)
            results.append({"node": node.name, "ok": True, "free": r.get("free")})
        except AgentError as exc:
            results.append({"node": node.name, "ok": False, "error": str(exc)})
    return results
