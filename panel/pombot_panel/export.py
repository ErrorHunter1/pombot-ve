"""Konfiguration exportieren: Einstellungen, Branding, Nodes, IP-Pools, Vorlagen, Benutzer, Server, Speicher …
als JSON-Datei (Adminbereich → Einstellungen → Export, oder per REST-API).

Geheimnisse (Passwort-Hashes, Node-Tokens und -Zertifikate, Zugangsdaten von Speichern, Discord-Secret,
Cloudflare-Token) sind standardmäßig nicht enthalten. Mit Geheimnissen exportieren geht nur mit Anmeldung im
Panel – nicht per API-Token. Hashes von API-Tokens werden nie exportiert.
"""
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import runtime
from .config import settings
from .db import get_db
from .models import (ApiToken, BackupSchedule, BackupTarget, FirewallConfig, Guest, IPAddress, IPPool, Node, Setting,
                     SharedStorage, Template, User)
from .security import audit, client_ip, require_admin

router = APIRouter(prefix="/api/admin/export", tags=["admin"])

SECTIONS = {
    "settings": "Einstellungen",
    "branding": "Branding & SEO",
    "nodes": "Nodes",
    "pools": "IP-Pools und Adressen",
    "templates": "Vorlagen",
    "users": "Benutzer und Kontingente",
    "guests": "Server (inkl. Firewall und Backup-Zeitplan)",
    "storages": "Gemeinsamer Speicher",
    "backup_targets": "Backup-Speicher",
    "api_tokens": "API-Tokens (ohne Token selbst)",
}
SECRET_COLUMNS = {User: {"password_hash"}, Node: {"token", "cert_pem"}}
SECRET_KEYS = {"password", "private_key", "secret_key"}  # in config_json von Speichern/Backup-Zielen
NEVER = {ApiToken: {"token_hash"}}


def _value(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _row(obj, secrets: bool, drop: set[str] = frozenset()) -> dict:
    cls = type(obj)
    skip = set(drop) | NEVER.get(cls, set()) | (set() if secrets else SECRET_COLUMNS.get(cls, set()))
    out = {}
    for col in sa_inspect(cls).columns:
        if col.key in skip:
            continue
        val = getattr(obj, col.key)
        if col.key.endswith("_json") and isinstance(val, str):
            try:
                val = json.loads(val or "null")
            except ValueError:
                pass
            if isinstance(val, dict) and not secrets:
                val = {k: ("***" if k in SECRET_KEYS and v else v) for k, v in val.items()}
            out[col.key[:-5]] = val
            continue
        out[col.key] = _value(val)
    return out


def build(db: Session, sections: list[str], secrets: bool) -> dict:
    data: dict = {"pombot_export": 1, "panel_version": settings.version, "exported_at": datetime.utcnow().isoformat() + "Z",
                  "base_url": settings.base_url, "includes_secrets": secrets, "sections": sections}
    node_names = {n.id: n.name for n in db.scalars(select(Node))}
    user_names = {u.id: u.username for u in db.scalars(select(User))}
    if "settings" in sections:
        view = runtime.view()
        if secrets:
            for name, kind in runtime.EDITABLE.items():
                if kind == "secret":
                    view[name] = getattr(settings, name, "")
        data["settings"] = {k: (sorted(v) if isinstance(v, set) else v) for k, v in view.items()}
    if "branding" in sections:
        row = db.get(Setting, "branding")
        data["branding"] = json.loads(row.value) if row else {}
    if "nodes" in sections:
        data["nodes"] = [_row(n, secrets, {"info_json"}) | {"info": {k: n.info.get(k) for k in (
            "hostname", "os", "kernel", "cpu_cores", "memory_total", "agent_version", "virtualization", "kvm", "bridges")}}
            for n in db.scalars(select(Node).order_by(Node.id))]
    if "pools" in sections:
        data["pools"] = [_row(p, secrets) | {"node": node_names.get(p.node_id), "addresses": [
            _row(a, secrets) for a in db.scalars(select(IPAddress).where(IPAddress.pool_id == p.id).order_by(IPAddress.id))]}
            for p in db.scalars(select(IPPool).order_by(IPPool.id))]
    if "templates" in sections:
        data["templates"] = [_row(t, secrets) for t in db.scalars(select(Template).order_by(Template.sort, Template.id))]
    if "users" in sections:
        data["users"] = [_row(u, secrets) for u in db.scalars(select(User).order_by(User.id))]
    if "guests" in sections:
        guests = []
        for g in db.scalars(select(Guest).order_by(Guest.vmid)):
            item = _row(g, secrets) | {"node": node_names.get(g.node_id), "owner": user_names.get(g.owner_id),
                                        "template": g.template.name if g.template else None,
                                        "storage": g.storage.name if g.storage else None,
                                        "ips": [{"address": ip.address, "pool": ip.pool.name} for ip in g.ips]}
            fw = db.get(FirewallConfig, g.id)
            item["firewall"] = _row(fw, secrets, {"guest_id", "rules_json"}) | {"rules": fw.rules} if fw else None
            bs = db.get(BackupSchedule, g.id)
            item["backup_schedule"] = _row(bs, secrets, {"guest_id"}) if bs else None
            guests.append(item)
        data["guests"] = guests
    if "storages" in sections:
        data["storages"] = [_row(s, secrets) for s in db.scalars(select(SharedStorage).order_by(SharedStorage.id))]
    if "backup_targets" in sections:
        data["backup_targets"] = [_row(t, secrets) for t in db.scalars(select(BackupTarget).order_by(BackupTarget.id))]
    if "api_tokens" in sections:
        data["api_tokens"] = [_row(t, secrets) | {"user": user_names.get(t.user_id)}
                              for t in db.scalars(select(ApiToken).order_by(ApiToken.id))]
    return data


@router.get("/sections")
def export_sections(user: User = Depends(require_admin)):
    return SECTIONS


@router.get("")
def export_config(request: Request, sections: str = "", secrets: bool = False, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    """Konfiguration als JSON-Datei. `sections`: Komma-Liste (leer = alles), `secrets=true` nur mit Anmeldung im Panel."""
    wanted = [s for s in (sections.split(",") if sections else SECTIONS) if s]
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        raise HTTPException(400, f"Unbekannte Bereiche: {', '.join(unknown)}")
    if secrets and getattr(request.state, "api_token", None):
        raise HTTPException(403, "Export mit Geheimnissen nur mit Anmeldung im Panel, nicht per API-Token")
    data = build(db, wanted, secrets)
    audit(db, user, "config-export", f"{', '.join(wanted)}{' inkl. Geheimnisse' if secrets else ''}", client_ip(request))
    db.commit()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="pombot-export-{stamp}.json"',
                             "Cache-Control": "no-store"})
