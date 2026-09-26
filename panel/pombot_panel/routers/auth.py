"""Anmeldung: lokal (Benutzername/Passwort) und per Discord OAuth2."""
import secrets
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import User, now
from ..security import audit, check_rate_limit, client_ip, current_user, register_fail, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
DISCORD_API = "https://discord.com/api/v10"


class LoginBody(BaseModel):
    username: str
    password: str


@router.get("/config")
def auth_config():
    from ..branding import public
    return {"discord": settings.discord_enabled, "registration": settings.registration,
            "version": settings.version, "brand": public()}


@router.post("/login")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    check_rate_limit(f"login:{ip}")
    user = db.scalar(select(User).where(func.lower(User.username) == body.username.strip().lower()))
    if not user or not verify_password(body.password, user.password_hash):
        register_fail(f"login:{ip}")
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    if not user.active:
        raise HTTPException(403, "Konto ist deaktiviert oder wartet auf Freischaltung")
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login = now()
    audit(db, user, "login", "lokal", ip)
    db.commit()
    return {"ok": True}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


def _discord_redirect(request: Request, link: bool) -> RedirectResponse:
    if not settings.discord_enabled:
        raise HTTPException(400, "Discord-Login ist nicht konfiguriert")
    state = secrets.token_urlsafe(24)
    request.session["discord_state"] = state
    request.session["discord_link"] = link
    scopes = ["identify", "email"] + (["guilds"] if settings.discord_guild_id else [])
    params = {"client_id": settings.discord_client_id, "redirect_uri": settings.discord_redirect_uri,
              "response_type": "code", "scope": " ".join(scopes), "state": state, "prompt": "none"}
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{urlencode(params)}")


@router.get("/discord/login")
def discord_login(request: Request):
    return _discord_redirect(request, link=False)


@router.get("/discord/link")
def discord_link(request: Request, user: User = Depends(current_user)):
    return _discord_redirect(request, link=True)


def _fail(msg: str) -> RedirectResponse:
    return RedirectResponse("/#/login?error=" + quote(msg))


@router.get("/discord/callback")
def discord_callback(request: Request, code: str = "", state: str = "", error: str = "",
                     db: Session = Depends(get_db)):
    expected = request.session.pop("discord_state", None)
    link = request.session.pop("discord_link", False)
    if error:
        return _fail("Discord-Anmeldung abgebrochen")
    if not expected or not secrets.compare_digest(expected, state) or not code:
        return _fail("Ungültige Anmeldeanfrage – bitte erneut versuchen")
    try:
        tok = httpx.post(f"{DISCORD_API}/oauth2/token", timeout=15, data={
            "client_id": settings.discord_client_id, "client_secret": settings.discord_client_secret,
            "grant_type": "authorization_code", "code": code, "redirect_uri": settings.discord_redirect_uri,
        })
        tok.raise_for_status()
        headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        me = httpx.get(f"{DISCORD_API}/users/@me", headers=headers, timeout=15)
        me.raise_for_status()
        profile = me.json()
        if settings.discord_guild_id:
            guilds = httpx.get(f"{DISCORD_API}/users/@me/guilds", headers=headers, timeout=15)
            guilds.raise_for_status()
            if not any(g.get("id") == settings.discord_guild_id for g in guilds.json()):
                return _fail("Du bist nicht Mitglied des erforderlichen Discord-Servers")
    except httpx.HTTPError:
        return _fail("Discord ist gerade nicht erreichbar")

    discord_id = str(profile["id"])
    avatar = (f"https://cdn.discordapp.com/avatars/{discord_id}/{profile['avatar']}.png?size=64"
              if profile.get("avatar") else None)
    ip = client_ip(request)

    if link:
        uid = request.session.get("uid")
        user = db.get(User, uid) if uid else None
        if not user:
            return _fail("Sitzung abgelaufen")
        other = db.scalar(select(User).where(User.discord_id == discord_id))
        if other and other.id != user.id:
            return RedirectResponse("/#/account?error=" + quote("Dieses Discord-Konto ist bereits mit einem anderen Benutzer verknüpft"))
        user.discord_id, user.avatar_url = discord_id, avatar
        audit(db, user, "discord-link", discord_id, ip)
        db.commit()
        return RedirectResponse("/#/account")

    user = db.scalar(select(User).where(User.discord_id == discord_id))
    if not user:
        if settings.registration == "closed":
            return _fail("Registrierung ist geschlossen – bitte an einen Administrator wenden")
        first_user = not db.scalar(select(func.count(User.id)))
        base = (profile.get("global_name") or profile.get("username") or "user").strip()[:40]
        name, n = base, 1
        while db.scalar(select(User).where(func.lower(User.username) == name.lower())):
            n += 1
            name = f"{base}{n}"
        is_admin = first_user or discord_id in settings.discord_admin_ids
        user = User(username=name, discord_id=discord_id, email=profile.get("email"), avatar_url=avatar,
                    role="admin" if is_admin else "user",
                    active=is_admin or settings.registration == "open",
                    max_guests=settings.default_max_guests, max_cores=settings.default_max_cores,
                    max_memory_mb=settings.default_max_memory_mb, max_disk_gb=settings.default_max_disk_gb,
                    max_ips=settings.default_max_ips)
        db.add(user)
        db.flush()
        audit(db, user, "register", f"Discord {discord_id}", ip)
    else:
        user.avatar_url = avatar
        if discord_id in settings.discord_admin_ids and user.role != "admin":
            user.role = "admin"
    db.commit()
    if not user.active:
        return RedirectResponse("/#/login?pending=1")
    request.session.clear()
    request.session["uid"] = user.id
    user.last_login = now()
    audit(db, user, "login", "Discord", ip)
    db.commit()
    return RedirectResponse("/#/dashboard")
