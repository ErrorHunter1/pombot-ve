"""Einladungen: Wer Benutzer verwalten darf, erstellt einen Einladungslink mit Rolle, Anmeldeart
(E-Mail/Passwort, Discord oder beides), Kontingent und Ablaufdatum. Der Eingeladene legt sich damit selbst ein
Konto an – auch wenn die freie Registrierung geschlossen ist. Jeder Link gilt nur einmal.
Der Link wird (noch) nicht per E-Mail verschickt, sondern im Panel angezeigt und weitergegeben.
"""
import hashlib
import json
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import perms
from .config import settings
from .db import get_db
from .models import Invite, Role, User, now
from .perms import require
from .security import audit, check_rate_limit, client_ip, hash_password, register_fail

router = APIRouter(prefix="/api/invites", tags=["users"])
USERNAME = r"^[A-Za-z0-9 _.\-]{2,40}$"
QUOTA_KEYS = ("max_guests", "max_cores", "max_memory_mb", "max_disk_gb", "max_ips")
METHODS = {"any": "E-Mail/Passwort oder Discord", "password": "Nur E-Mail/Passwort", "discord": "Nur Discord"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_url(token: str) -> str:
    return f"{settings.base_url}/#/invite/{token}"


def find_open(db: Session, token: str) -> Invite:
    inv = db.scalar(select(Invite).where(Invite.token_hash == _hash(token or "")))
    if not inv:
        raise HTTPException(404, "Einladung nicht gefunden")
    if inv.used_at:
        raise HTTPException(410, "Diese Einladung wurde bereits verwendet")
    if inv.expires_at and inv.expires_at < now():
        raise HTTPException(410, "Diese Einladung ist abgelaufen")
    return inv


def new_user_from(db: Session, inv: Invite, username: str, **fields) -> User:
    """Legt den Benutzer aus einer Einladung an und markiert sie als verwendet."""
    base = {k: getattr(settings, f"default_{k}") for k in QUOTA_KEYS}
    base.update({k: int(v) for k, v in inv.quota.items() if k in QUOTA_KEYS})
    user = User(username=username, role=inv.role if db.scalar(select(Role.id).where(Role.key == inv.role)) else "user",
                login_methods=inv.login_methods, active=True, **base, **fields)
    db.add(user)
    db.flush()
    inv.used_at, inv.used_by = now(), user.id
    return user


def unique_name(db: Session, base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 _.\-]", "", base).strip()[:36] or "user"
    if len(base) < 2:
        base = f"user{base}"
    name, n = base, 1
    while db.scalar(select(User.id).where(func.lower(User.username) == name.lower())):
        n += 1
        name = f"{base}{n}"
    return name


# ---------------------------------------------------------------- Verwaltung (users.manage)

def invite_dict(db: Session, inv: Invite, names: dict) -> dict:
    return {"id": inv.id, "prefix": inv.prefix, "email": inv.email, "note": inv.note, "role": inv.role,
            "role_name": perms.role_label(db, inv.role), "login_methods": inv.login_methods, "quota": inv.quota,
            "created_by": names.get(inv.created_by), "created_at": inv.created_at.isoformat() + "Z",
            "expires_at": inv.expires_at.isoformat() + "Z" if inv.expires_at else None,
            "expired": bool(inv.expires_at and inv.expires_at < now() and not inv.used_at),
            "used_at": inv.used_at.isoformat() + "Z" if inv.used_at else None, "used_by": names.get(inv.used_by)}


@router.get("")
def list_invites(user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    names = dict(db.execute(select(User.id, User.username)).all())
    return [invite_dict(db, i, names) for i in db.scalars(select(Invite).order_by(Invite.id.desc()).limit(200))]


class InviteBody(BaseModel):
    email: str | None = Field(default=None, max_length=255)
    note: str = Field(default="", max_length=255)
    role: str = Field(default="user", max_length=16)
    login_methods: str = Field(default="any", pattern="^(any|password|discord)$")
    expires_days: int | None = Field(default=7, ge=1, le=365)
    quota: dict = {}


@router.post("")
def create_invite(body: InviteBody, request: Request, user: User = Depends(require("users.manage")),
                  db: Session = Depends(get_db)):
    if not db.scalar(select(Role.id).where(Role.key == body.role)):
        raise HTTPException(400, "Rolle nicht gefunden")
    if not perms.can_assign(db, user, body.role):
        raise HTTPException(403, "Diese Rolle darfst du nicht vergeben (sie hat mehr Rechte als deine)")
    if body.login_methods == "discord" and not settings.discord_enabled:
        raise HTTPException(400, "Discord-Login ist nicht eingerichtet (Einstellungen → Discord-Login)")
    quota = {}
    for k, v in (body.quota or {}).items():
        if k not in QUOTA_KEYS or v in (None, ""):
            continue
        try:
            quota[k] = max(0, min(int(v), 100_000_000))
        except (TypeError, ValueError):
            raise HTTPException(400, f"Kontingent {k} muss eine Zahl sein")
    token = secrets.token_urlsafe(24)
    inv = Invite(token_hash=_hash(token), prefix=token[:8], email=(body.email or "").strip() or None,
                 note=body.note.strip(), role=body.role, login_methods=body.login_methods, quota_json=json.dumps(quota),
                 created_by=user.id, expires_at=now() + timedelta(days=body.expires_days) if body.expires_days else None)
    db.add(inv)
    audit(db, user, "invite-create", f"{inv.email or inv.note or inv.prefix}: Rolle {body.role}, {METHODS[body.login_methods]}",
          client_ip(request))
    db.commit()
    names = {user.id: user.username}
    return {**invite_dict(db, inv, names), "url": invite_url(token)}


@router.delete("/{invite_id}")
def delete_invite(invite_id: int, request: Request, user: User = Depends(require("users.manage")),
                  db: Session = Depends(get_db)):
    inv = db.get(Invite, invite_id)
    if not inv:
        raise HTTPException(404, "Einladung nicht gefunden")
    audit(db, user, "invite-delete", inv.email or inv.note or inv.prefix, client_ip(request))
    db.delete(inv)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- öffentlich: Einladung ansehen und annehmen

@router.get("/public/{token}")
def invite_info(token: str, request: Request, db: Session = Depends(get_db)):
    check_rate_limit(f"invite:{client_ip(request)}", limit=30)
    try:
        inv = find_open(db, token)
    except HTTPException:
        register_fail(f"invite:{client_ip(request)}")
        raise
    from .branding import public
    return {"email": inv.email, "role_name": perms.role_label(db, inv.role), "login_methods": inv.login_methods,
            "discord": settings.discord_enabled and inv.login_methods in ("any", "discord"),
            "password": inv.login_methods in ("any", "password"),
            "expires_at": inv.expires_at.isoformat() + "Z" if inv.expires_at else None, "brand": public()}


class AcceptBody(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=10, max_length=128)
    email: str | None = Field(default=None, max_length=255)


@router.post("/public/{token}/accept")
def invite_accept(token: str, body: AcceptBody, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    check_rate_limit(f"invite:{ip}", limit=30)
    inv = find_open(db, token)
    if inv.login_methods == "discord":
        raise HTTPException(400, "Diese Einladung gilt nur für die Anmeldung mit Discord")
    if db.scalar(select(User.id).where(func.lower(User.username) == body.username.strip().lower())):
        raise HTTPException(400, "Benutzername bereits vergeben")
    email = (body.email or inv.email or "").strip() or None
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Ungültige E-Mail-Adresse")
    user = new_user_from(db, inv, body.username.strip(), password_hash=hash_password(body.password), email=email)
    audit(db, user, "register", f"Einladung {inv.prefix} (Rolle {inv.role})", ip)
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login = now()
    db.commit()
    return {"ok": True}
