"""REST-API: Zugriff per API-Token, Ein/Aus-Schalter und OpenAPI-Beschreibung für die Dokumentation.

Die REST-API ist dieselbe API, die auch die Weboberfläche nutzt (/api/…). Statt mit Sitzungs-Cookie
meldet man sich mit einem Token an:   Authorization: Bearer pbt_…
- Nur Administratoren können Tokens anlegen; ein Token handelt mit den Rechten seines Admins.
- Tokens können nur lesend sein (GET) und ein Ablaufdatum haben. Gespeichert wird nur ein Hash.
- Die API muss im Adminbereich eingeschaltet sein (Standard: aus).
- Tokens selbst und der Ein/Aus-Schalter lassen sich nur mit Anmeldung im Panel verwalten, nicht per Token.
"""
import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import ApiToken, User, now
from .security import audit, client_ip, require_admin

PREFIX = "pbt_"
MANAGE_PATH = "/api/admin/api"  # Verwaltung – nie per Token
router = APIRouter(prefix=MANAGE_PATH, tags=["rest-api"])

# Reihenfolge und deutsche Titel der Abschnitte in der Dokumentation
TAG_TITLES = {
    "guests": "Server", "extras": "Firewall & Backup-Zeitpläne", "apps": "Anwendungen", "nodes": "Nodes",
    "pools": "IP-Pools", "system": "Vorlagen, Aufgaben & Protokoll", "users": "Benutzer & Konto",
    "storages": "Gemeinsamer Speicher", "backup-targets": "Backup-Speicher", "updates": "Updates",
    "branding": "Branding & SEO", "admin": "Einstellungen, DNS & Domain", "auth": "Anmeldung",
}
TAG_INFO = {
    "guests": "Server (VMs und Container): anlegen, steuern, ändern, klonen, Snapshots, Backups, Netzwerk, Umzug.",
    "extras": "Firewall und Backup-Zeitpläne pro Server.",
    "nodes": "Nodes (Virtualisierungs-Hosts): Liste, Details, ISOs, Images, Backups.",
    "pools": "IP-Pools und Adressvergabe.",
    "users": "Benutzer, Kontingente und das eigene Konto.",
    "system": "Vorlagen, Aufgaben (Tasks), Audit-Protokoll, Dashboard.",
    "apps": "Katalog der Anwendungen, die beim Erstellen mitinstalliert werden können.",
    "storages": "Gemeinsamer Speicher (NFS, SMB, CephFS) für HA und schnelle Migration.",
    "backup-targets": "Externe Backup-Speicher (SFTP, S3, NFS, SMB).",
    "updates": "Versionsprüfung und Updates von Panel und Nodes.",
    "branding": "Name, Meta-Angaben, Indexierung und Bilder des Panels.",
    "admin": "Einstellungen, Cloudflare-DNS, Domain & HTTPS.",
    "auth": "Anmeldung im Panel (für die REST-API nicht nötig – dort gilt das Token).",
}
HIDDEN_PREFIXES = (MANAGE_PATH, "/api/auth/discord")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_from_token(request: Request, db: Session, raw: str) -> User:
    """Prüft ein Bearer-Token und liefert den Admin, dem es gehört. Wirft HTTPException."""
    from .security import check_rate_limit, register_fail
    ip = client_ip(request)
    if not getattr(settings, "api_enabled", False):
        raise HTTPException(403, "Die REST-API ist deaktiviert (Adminbereich → Einstellungen → REST-API)")
    check_rate_limit(f"token:{ip}", limit=20)
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(raw.strip())))
    if not token or (token.expires_at and token.expires_at < now()):
        register_fail(f"token:{ip}")
        raise HTTPException(401, "Ungültiges oder abgelaufenes API-Token")
    user = db.get(User, token.user_id)
    if not user or not user.active or not user.is_admin:
        raise HTTPException(401, "Das Token gehört zu keinem aktiven Administrator mehr")
    path = request.url.path
    if path.startswith(MANAGE_PATH):
        raise HTTPException(403, "Tokens und API-Einstellungen lassen sich nur im Panel verwalten")
    if token.read_only and request.method not in ("GET", "HEAD"):
        raise HTTPException(403, "Dieses Token darf nur lesen")
    if not token.last_used_at or (now() - token.last_used_at).total_seconds() > 60:
        token.last_used_at = now()
        token.last_ip = ip
        db.commit()
    request.state.api_token = token.name
    return user


# ---------------------------------------------------------------- Verwaltung (nur mit Anmeldung im Panel)

def _token_dict(t: ApiToken, names: dict) -> dict:
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "user": names.get(t.user_id), "read_only": t.read_only,
            "created_at": t.created_at.isoformat() + "Z",
            "expires_at": t.expires_at.isoformat() + "Z" if t.expires_at else None,
            "expired": bool(t.expires_at and t.expires_at < now()),
            "last_used_at": t.last_used_at.isoformat() + "Z" if t.last_used_at else None, "last_ip": t.last_ip}


@router.get("")
def api_status(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    names = dict(db.execute(select(User.id, User.username)).all())
    tokens = [_token_dict(t, names) for t in db.scalars(select(ApiToken).order_by(ApiToken.id.desc()))]
    return {"enabled": bool(getattr(settings, "api_enabled", False)), "base_url": settings.base_url, "tokens": tokens}


class ToggleBody(BaseModel):
    enabled: bool


@router.put("")
def api_toggle(body: ToggleBody, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from . import runtime
    runtime.save(db, {"api_enabled": body.enabled})
    audit(db, user, "api-toggle", "REST-API " + ("eingeschaltet" if body.enabled else "ausgeschaltet"), client_ip(request))
    db.commit()
    return api_status(user, db)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    read_only: bool = False
    expires_days: int | None = Field(default=None, ge=1, le=3650)  # None = läuft nicht ab


@router.post("/tokens")
def token_create(body: TokenBody, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    raw = PREFIX + secrets.token_urlsafe(32)
    t = ApiToken(name=body.name.strip(), user_id=user.id, token_hash=hash_token(raw), prefix=raw[:10],
                 read_only=body.read_only,
                 expires_at=now() + timedelta(days=body.expires_days) if body.expires_days else None)
    db.add(t)
    audit(db, user, "api-token-create", f"{t.name} ({'nur lesen' if t.read_only else 'lesen+schreiben'})", client_ip(request))
    db.commit()
    return {**_token_dict(t, {user.id: user.username}), "token": raw}


@router.delete("/tokens/{token_id}")
def token_delete(token_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    t = db.get(ApiToken, token_id)
    if not t:
        raise HTTPException(404, "Token nicht gefunden")
    audit(db, user, "api-token-delete", t.name, client_ip(request))
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.get("/openapi.json")
def openapi_spec(request: Request, user: User = Depends(require_admin)):
    """OpenAPI-Beschreibung aller REST-Endpunkte (für die Dokumentation im Panel)."""
    import copy
    from .api_docs import apply
    spec = copy.deepcopy(request.app.openapi())
    apply(spec)
    paths = {p: v for p, v in spec.get("paths", {}).items()
             if p.startswith("/api/") and not p.startswith(HIDDEN_PREFIXES)}
    used = {tag for ops in paths.values() for op in ops.values() for tag in op.get("tags", [])}
    return {
        **spec,
        "info": {"title": "PomBot VE REST-API", "version": settings.version,
                 "description": "Anmeldung per `Authorization: Bearer <Token>`. Tokens legen Administratoren im Panel an."},
        "servers": [{"url": settings.base_url}],
        "paths": paths,
        "tags": [{"name": t, "x-displayName": TAG_TITLES.get(t, t), "description": TAG_INFO.get(t, "")}
                 for t in sorted(used, key=lambda t: (list(TAG_TITLES).index(t) if t in TAG_TITLES else 99, t))],
        "components": {**spec.get("components", {}),
                       "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "pbt_…"}}},
        "security": [{"bearer": []}],
    }
