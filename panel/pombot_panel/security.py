"""Passwörter, Sessions, Rechteprüfung, Audit-Log."""
import hashlib
import hmac
import os
import secrets
import string
import time
from collections import defaultdict

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuditLog, Guest, User


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_hex, digest_hex = stored.split("$", 2)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


# ---------------------------------------------------------------- Login-Bremse

_FAILS: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(key: str, limit: int = 10, window: int = 600) -> None:
    now = time.time()
    _FAILS[key] = [t for t in _FAILS[key] if now - t < window]
    if len(_FAILS[key]) >= limit:
        raise HTTPException(429, "Zu viele Fehlversuche – bitte später erneut versuchen")


def register_fail(key: str) -> None:
    _FAILS[key].append(time.time())


# ---------------------------------------------------------------- Abhängigkeiten

def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        from .api_tokens import user_from_token
        return user_from_token(request, db, auth[7:])  # REST-API mit Token
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        request.session.clear()
        raise HTTPException(401, "Nicht angemeldet")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Nur für Administratoren")
    return user


def guest_for(db: Session, user: User, guest_id: int) -> Guest:
    guest = db.get(Guest, guest_id)
    if not guest or (not user.can("guests.all") and guest.owner_id != user.id):
        raise HTTPException(404, "Server nicht gefunden")
    return guest


def audit(db: Session, user: User | None, action: str, detail: str = "", ip: str = "") -> None:
    db.add(AuditLog(user_id=user.id if user else None, username=user.username if user else "",
                    action=action, detail=detail[:2000], ip=ip))
