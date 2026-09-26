"""Rollen und Rechte.

Jeder Benutzer hat genau eine Rolle (User.role = Role.key). Eingebaut sind:
  admin – darf alles (auch Rollen verwalten); lässt sich nicht ändern
  user  – normaler Kunde: eigene Server im Rahmen seines Kontingents, keine weiteren Rechte
Weitere Rollen legt ein Admin an und vergibt einzelne Rechte, z. B. „Node-Verwalter“ mit nodes.manage.

Schutz vor Rechteausweitung: Wer Benutzer verwalten darf (users.manage), aber kein Admin ist, kann nur Rollen
vergeben, deren Rechte er selbst besitzt, und keine Benutzer mit mehr Rechten ändern oder löschen.
"""
import json

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Role, User

PERMISSIONS: dict[str, str] = {
    "guests.all": "Alle Server sehen und verwalten – auch die anderer Benutzer (Besitzer ändern, erzwungen löschen, MAC, HA, Umzug)",
    "quota.unlimited": "Ohne Kontingent; alle IP-Pools, ausgeblendeten Vorlagen und gesperrten Nodes nutzen",
    "nodes.manage": "Nodes verwalten: hinzufügen, entfernen, ISOs, Images, Backups, Root-Shell",
    "pools.manage": "IP-Pools und Adressen verwalten",
    "templates.manage": "Vorlagen verwalten",
    "storage.manage": "Gemeinsamen Speicher und Backup-Speicher verwalten",
    "users.manage": "Benutzer und Einladungen verwalten",
    "settings.manage": "Einstellungen, Discord-Login, Branding & SEO, Domain, Cloudflare-DNS",
    "api.manage": "REST-API ein/aus und eigene API-Tokens (Token hat die Rechte seines Besitzers)",
    "config.export": "Konfiguration exportieren",
    "updates.manage": "Updates von Panel und Nodes starten",
    "audit.view": "Audit-Protokoll und alle Aufgaben sehen",
}
BUILTIN = {
    "admin": ("Administrator", "Darf alles, auch Rollen verwalten.", list(PERMISSIONS)),
    "user": ("Benutzer", "Eigene Server im Rahmen des Kontingents.", []),
}
_cache: dict[str, set[str]] = {}


def seed(db: Session) -> None:
    """Eingebaute Rollen anlegen (beim Start)."""
    for key, (name, desc, perms) in BUILTIN.items():
        role = db.scalar(select(Role).where(Role.key == key))
        if not role:
            db.add(Role(key=key, name=name, description=desc, permissions=json.dumps(perms), builtin=True))
    db.commit()
    _cache.clear()


def perms_of(db: Session, role_key: str) -> set[str]:
    if role_key == "admin":
        return set(PERMISSIONS)
    if role_key not in _cache:
        role = db.scalar(select(Role).where(Role.key == role_key))
        _cache[role_key] = {p for p in (json.loads(role.permissions or "[]") if role else []) if p in PERMISSIONS}
    return _cache[role_key]


def invalidate() -> None:
    _cache.clear()


def can(user: User, perm: str, db: Session | None = None) -> bool:
    if user.role == "admin":
        return True
    from sqlalchemy.orm import object_session
    db = db or object_session(user)
    return db is not None and perm in perms_of(db, user.role)


def require(perm: str):
    """FastAPI-Abhängigkeit: angemeldeter Benutzer mit diesem Recht."""
    from .security import current_user

    def dep(user: User = Depends(current_user)) -> User:
        if not can(user, perm):
            raise HTTPException(403, f"Keine Berechtigung ({PERMISSIONS.get(perm, perm)})")
        return user
    return dep


def require_any(*perms: str):
    from .security import current_user

    def dep(user: User = Depends(current_user)) -> User:
        if not any(can(user, p) for p in perms):
            raise HTTPException(403, "Keine Berechtigung")
        return user
    return dep


def can_assign(db: Session, actor: User, role_key: str) -> bool:
    """Darf actor diese Rolle vergeben? Admins alles, andere nur Rollen ohne zusätzliche Rechte."""
    if actor.role == "admin":
        return True
    if role_key == "admin" or not db.scalar(select(Role.id).where(Role.key == role_key)):
        return False
    return perms_of(db, role_key) <= perms_of(db, actor.role)


def can_manage(db: Session, actor: User, target: User) -> bool:
    """Darf actor diesen Benutzer ändern/löschen? Nicht-Admins keine Admins oder Benutzer mit mehr Rechten."""
    if actor.role == "admin":
        return True
    return target.role != "admin" and perms_of(db, target.role) <= perms_of(db, actor.role)


def role_label(db: Session, key: str) -> str:
    role = db.scalar(select(Role).where(Role.key == key))
    return role.name if role else key
