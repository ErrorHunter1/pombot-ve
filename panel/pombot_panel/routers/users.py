"""Benutzerverwaltung (Admin) und eigenes Konto."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ops
from ..config import settings
from ..db import get_db
import json

from .. import perms
from ..models import Guest, Invite, Role, User
from ..perms import require, require_any
from ..security import (audit, client_ip, current_user, generate_password, hash_password, require_admin,
                        verify_password)

router = APIRouter(prefix="/api", tags=["users"])
USERNAME = r"^[A-Za-z0-9 _.\-]{2,40}$"


def user_dict(db: Session, u: User, full: bool = True) -> dict:
    data = {"id": u.id, "username": u.username, "role": u.role, "role_name": perms.role_label(db, u.role),
            "active": u.active, "avatar_url": u.avatar_url, "discord": bool(u.discord_id), "login_methods": u.login_methods}
    if full:
        data.update({
            "email": u.email, "discord_id": u.discord_id, "has_password": bool(u.password_hash),
            "created_at": u.created_at.isoformat() + "Z",
            "last_login": u.last_login.isoformat() + "Z" if u.last_login else None,
            "quota": ops.quota(u), "usage": ops.usage(db, u), "ssh_keys": u.ssh_keys,
        })
    return data


# ---------------------------------------------------------------- Eigenes Konto

@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = user_dict(db, user)
    data["discord_enabled"] = settings.discord_enabled
    data["permissions"] = sorted(perms.perms_of(db, user.role))
    return data


class MeBody(BaseModel):
    ssh_keys: str | None = Field(default=None, max_length=20000)
    email: str | None = Field(default=None, max_length=255)


@router.patch("/me")
def update_me(body: MeBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.ssh_keys is not None:
        user.ssh_keys = body.ssh_keys.strip()
    if body.email is not None:
        user.email = body.email.strip() or None
    db.commit()
    return user_dict(db, user)


class PasswordBody(BaseModel):
    current: str = ""
    new: str = Field(min_length=10, max_length=128)


@router.post("/me/password")
def change_password(body: PasswordBody, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if user.password_hash and not verify_password(body.current, user.password_hash):
        raise HTTPException(400, "Aktuelles Passwort ist falsch")
    user.password_hash = hash_password(body.new)
    audit(db, user, "password-change", "", client_ip(request))
    db.commit()
    return {"ok": True}


@router.post("/me/unlink-discord")
def unlink_discord(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.password_hash:
        raise HTTPException(400, "Bitte zuerst ein Passwort setzen, sonst kannst du dich nicht mehr anmelden")
    user.discord_id = None
    audit(db, user, "discord-unlink", "", client_ip(request))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Benutzerverwaltung (Recht users.manage)

LOGIN = "^(any|password|discord)$"


def _check_role(db: Session, actor: User, role_key: str) -> None:
    if not db.scalar(select(Role.id).where(Role.key == role_key)):
        raise HTTPException(400, "Rolle nicht gefunden")
    if not perms.can_assign(db, actor, role_key):
        raise HTTPException(403, "Diese Rolle darfst du nicht vergeben (sie hat mehr Rechte als deine)")


@router.get("/users")
def list_users(user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    return [{**user_dict(db, u), "manageable": perms.can_manage(db, user, u)}
            for u in db.scalars(select(User).order_by(User.id))]


@router.get("/users/brief")
def list_users_brief(user: User = Depends(require_any("users.manage", "guests.all")), db: Session = Depends(get_db)):
    return [{"id": u.id, "username": u.username} for u in db.scalars(select(User).order_by(User.username))]


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str | None = Field(default=None, max_length=128)
    role: str = Field(default="user", max_length=16)
    login_methods: str = Field(default="any", pattern=LOGIN)
    email: str | None = Field(default=None, max_length=255)


@router.post("/users")
def create_user(body: UserCreate, request: Request, admin: User = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    if db.scalar(select(User).where(func.lower(User.username) == body.username.lower())):
        raise HTTPException(400, "Benutzername bereits vergeben")
    _check_role(db, admin, body.role)
    password = body.password or generate_password()
    u = User(username=body.username, password_hash=hash_password(password), role=body.role,
             login_methods=body.login_methods, email=(body.email or "").strip() or None,
             max_guests=settings.default_max_guests, max_cores=settings.default_max_cores,
             max_memory_mb=settings.default_max_memory_mb, max_disk_gb=settings.default_max_disk_gb,
             max_ips=settings.default_max_ips)
    db.add(u)
    audit(db, admin, "user-create", f"{body.username} ({body.role})", client_ip(request))
    db.commit()
    return {"id": u.id, "password": password}


class UserPatch(BaseModel):
    username: str | None = Field(default=None, pattern=USERNAME)
    role: str | None = Field(default=None, max_length=16)
    login_methods: str | None = Field(default=None, pattern=LOGIN)
    active: bool | None = None
    max_guests: int | None = Field(default=None, ge=0, le=10000)
    max_cores: int | None = Field(default=None, ge=0, le=100000)
    max_memory_mb: int | None = Field(default=None, ge=0, le=100_000_000)
    max_disk_gb: int | None = Field(default=None, ge=0, le=10_000_000)
    max_ips: int | None = Field(default=None, ge=0, le=100000)
    reset_password: bool = False


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, admin: User = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Benutzer nicht gefunden")
    if not perms.can_manage(db, admin, u):
        raise HTTPException(403, "Diesen Benutzer darfst du nicht ändern (er hat mehr Rechte als du)")
    if u.id == admin.id and ((body.role and body.role != u.role) or body.active is False):
        raise HTTPException(400, "Du kannst deine eigene Rolle nicht ändern und dich nicht deaktivieren")
    if body.role and body.role != u.role:
        _check_role(db, admin, body.role)
        if u.role == "admin" and not db.scalar(select(User.id).where(User.role == "admin", User.id != u.id, User.active.is_(True))):
            raise HTTPException(400, "Es muss mindestens ein aktiver Administrator bleiben")
    if body.login_methods == "discord" and not u.discord_id:
        raise HTTPException(400, "Das Konto ist mit keinem Discord-Konto verknüpft – sonst könnte es sich nicht mehr anmelden")
    if body.username and body.username.lower() != u.username.lower():
        if db.scalar(select(User).where(func.lower(User.username) == body.username.lower())):
            raise HTTPException(400, "Benutzername bereits vergeben")
    changes = body.model_dump(exclude_none=True, exclude={"reset_password"})
    for key, value in changes.items():
        setattr(u, key, value)
    result = {}
    if body.reset_password:
        password = generate_password()
        u.password_hash = hash_password(password)
        result["password"] = password
    audit(db, admin, "user-update", f"{u.username}: {changes}{' +Passwort' if body.reset_password else ''}",
          client_ip(request))
    db.commit()
    return {**user_dict(db, u), **result}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, admin: User = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Benutzer nicht gefunden")
    if u.id == admin.id:
        raise HTTPException(400, "Du kannst dich nicht selbst löschen")
    if not perms.can_manage(db, admin, u):
        raise HTTPException(403, "Diesen Benutzer darfst du nicht löschen (er hat mehr Rechte als du)")
    if db.scalar(select(Guest.id).where(Guest.owner_id == u.id)):
        raise HTTPException(400, "Der Benutzer besitzt noch Server – bitte löschen oder übertragen")
    audit(db, admin, "user-delete", u.username, client_ip(request))
    db.delete(u)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Rollen

def role_dict(db: Session, r: Role) -> dict:
    return {"id": r.id, "key": r.key, "name": r.name, "description": r.description, "builtin": r.builtin,
            "permissions": sorted(perms.perms_of(db, r.key)),
            "users": db.scalar(select(func.count(User.id)).where(User.role == r.key)) or 0}


@router.get("/roles")
def list_roles(user: User = Depends(require_any("users.manage", "guests.all")), db: Session = Depends(get_db)):
    roles = db.scalars(select(Role).order_by(Role.builtin.desc(), Role.name))
    return {"roles": [{**role_dict(db, r), "assignable": perms.can_assign(db, user, r.key)} for r in roles],
            "permissions": perms.PERMISSIONS}


class RoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    permissions: list[str] = []


def _clean_perms(values: list[str]) -> list[str]:
    unknown = [p for p in values if p not in perms.PERMISSIONS]
    if unknown:
        raise HTTPException(400, f"Unbekannte Rechte: {', '.join(unknown)}")
    return sorted(set(values))


@router.post("/roles")
def create_role(body: RoleBody, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if db.scalar(select(Role.id).where(func.lower(Role.name) == body.name.strip().lower())):
        raise HTTPException(400, "Es gibt schon eine Rolle mit diesem Namen")
    r = Role(key="tmp", name=body.name.strip(), description=body.description.strip(),
             permissions=json.dumps(_clean_perms(body.permissions)))
    db.add(r)
    db.flush()
    r.key = f"r{r.id}"
    audit(db, user, "role-create", f"{r.name}: {', '.join(_clean_perms(body.permissions)) or 'keine Rechte'}", client_ip(request))
    db.commit()
    perms.invalidate()
    return role_dict(db, r)


@router.put("/roles/{role_id}")
def update_role(role_id: int, body: RoleBody, request: Request, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    r = db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Rolle nicht gefunden")
    if r.builtin:
        raise HTTPException(400, "Eingebaute Rollen lassen sich nicht ändern – lege eine eigene Rolle an")
    r.name, r.description = body.name.strip(), body.description.strip()
    r.permissions = json.dumps(_clean_perms(body.permissions))
    audit(db, user, "role-update", f"{r.name}: {', '.join(_clean_perms(body.permissions)) or 'keine Rechte'}", client_ip(request))
    db.commit()
    perms.invalidate()
    return role_dict(db, r)


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Rolle nicht gefunden")
    if r.builtin:
        raise HTTPException(400, "Eingebaute Rollen lassen sich nicht löschen")
    if db.scalar(select(User.id).where(User.role == r.key)):
        raise HTTPException(400, "Die Rolle ist noch Benutzern zugewiesen")
    if db.scalar(select(Invite.id).where(Invite.role == r.key, Invite.used_at.is_(None))):
        raise HTTPException(400, "Offene Einladungen verwenden diese Rolle noch")
    audit(db, user, "role-delete", r.name, client_ip(request))
    db.delete(r)
    db.commit()
    perms.invalidate()
    return {"ok": True}
