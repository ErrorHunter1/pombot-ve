"""Benutzerverwaltung (Admin) und eigenes Konto."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ops
from ..config import settings
from ..db import get_db
from ..models import Guest, User
from ..security import (audit, client_ip, current_user, generate_password, hash_password, require_admin,
                        verify_password)

router = APIRouter(prefix="/api", tags=["users"])
USERNAME = r"^[A-Za-z0-9 _.\-]{2,40}$"


def user_dict(db: Session, u: User, full: bool = True) -> dict:
    data = {"id": u.id, "username": u.username, "role": u.role, "active": u.active,
            "avatar_url": u.avatar_url, "discord": bool(u.discord_id)}
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


# ---------------------------------------------------------------- Admin

@router.get("/users")
def list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [user_dict(db, u) for u in db.scalars(select(User).order_by(User.id))]


@router.get("/users/brief")
def list_users_brief(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [{"id": u.id, "username": u.username} for u in db.scalars(select(User).order_by(User.username))]


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str | None = Field(default=None, max_length=128)
    role: str = Field(default="user", pattern="^(admin|user)$")


@router.post("/users")
def create_user(body: UserCreate, request: Request, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    if db.scalar(select(User).where(func.lower(User.username) == body.username.lower())):
        raise HTTPException(400, "Benutzername bereits vergeben")
    password = body.password or generate_password()
    u = User(username=body.username, password_hash=hash_password(password), role=body.role,
             max_guests=settings.default_max_guests, max_cores=settings.default_max_cores,
             max_memory_mb=settings.default_max_memory_mb, max_disk_gb=settings.default_max_disk_gb,
             max_ips=settings.default_max_ips)
    db.add(u)
    audit(db, admin, "user-create", body.username, client_ip(request))
    db.commit()
    return {"id": u.id, "password": password}


class UserPatch(BaseModel):
    username: str | None = Field(default=None, pattern=USERNAME)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    active: bool | None = None
    max_guests: int | None = Field(default=None, ge=0, le=10000)
    max_cores: int | None = Field(default=None, ge=0, le=100000)
    max_memory_mb: int | None = Field(default=None, ge=0, le=100_000_000)
    max_disk_gb: int | None = Field(default=None, ge=0, le=10_000_000)
    max_ips: int | None = Field(default=None, ge=0, le=100000)
    reset_password: bool = False


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Benutzer nicht gefunden")
    if u.id == admin.id and (body.role == "user" or body.active is False):
        raise HTTPException(400, "Du kannst dir selbst keine Adminrechte entziehen oder dich deaktivieren")
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
def delete_user(user_id: int, request: Request, admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Benutzer nicht gefunden")
    if u.id == admin.id:
        raise HTTPException(400, "Du kannst dich nicht selbst löschen")
    if db.scalar(select(Guest.id).where(Guest.owner_id == u.id)):
        raise HTTPException(400, "Der Benutzer besitzt noch Server – bitte löschen oder übertragen")
    audit(db, admin, "user-delete", u.username, client_ip(request))
    db.delete(u)
    db.commit()
    return {"ok": True}
