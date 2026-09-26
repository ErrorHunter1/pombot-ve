"""Anwendungs-Katalog: beim Erstellen eines Servers z. B. mailcow oder Pterodactyl mitinstallieren.

Jede Anwendung besteht aus Metadaten (hier) und einem Bash-Skript in app_scripts/<id>.sh. Beim Anlegen
wird daraus ein Skript gebaut: Eingaben als Umgebungsvariablen (sicher gequotet) + _common.sh + Skript.
Der Agent legt es in den Server und startet es im Hintergrund (siehe agent/pombot_agent/apps.py).
"""
import json
import re
import shlex
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .models import Guest, Template, User
from .security import current_user

SCRIPTS = Path(__file__).parent / "app_scripts"
LINUX = ("debian", "ubuntu")

FIELD_RULES = {
    "domain": (r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$", "gültige Domain, z. B. mail.example.de"),
    "email": (r"^[^@\s'\"`$\\]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$", "gültige E-Mail-Adresse"),
    "username": (r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$", "3–32 Zeichen: Buchstaben, Ziffern, . _ -"),
    "tz": (r"^[A-Za-z]+(/[A-Za-z0-9_+-]+){0,2}$", "Zeitzone, z. B. Europe/Berlin"),
    "url": (r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$", "Adresse, z. B. https://panel.example.de"),
    "token": (r"^[A-Za-z0-9._-]{8,256}$", "Token (Buchstaben, Ziffern, . _ -)"),
    "number": (r"^[0-9]{1,9}$", "Zahl"),
    "bool": (r"^[01]$", "an/aus"),
}

# type: kvm/lxc erlaubt; min: Mindestressourcen; fields: Eingaben (env = Variablenname im Skript)
CATALOG = [
    {"id": "pterodactyl-panel", "name": "Pterodactyl Panel", "category": "Gameserver",
     "description": "Verwaltungsoberfläche für Gameserver (Minecraft, Rust, CS2 …) mit nginx, MariaDB, Redis und PHP.",
     "types": ["kvm", "lxc"], "min": {"cores": 1, "memory_mb": 2048, "disk_gb": 10},
     "fields": [
         {"env": "APP_DOMAIN", "label": "Domain des Panels", "kind": "domain", "required": False, "hostname": True,
          "hint": "optional – sonst über die IP erreichbar"},
         {"env": "APP_EMAIL", "label": "E-Mail des Admins", "kind": "email", "required": True},
         {"env": "APP_ADMIN", "label": "Admin-Benutzername", "kind": "username", "required": True, "default": "admin"},
         {"env": "APP_TZ", "label": "Zeitzone", "kind": "tz", "required": True, "default": "Europe/Berlin"},
         {"env": "APP_SSL", "label": "HTTPS mit Let's Encrypt (Domain muss auf den Server zeigen)", "kind": "bool", "default": "0"},
     ]},
    {"id": "pterodactyl-wings", "name": "Pterodactyl Wings", "category": "Gameserver",
     "description": "Daemon, auf dem die Gameserver laufen (Docker). Wird mit einem Pterodactyl Panel verbunden.",
     "types": ["kvm"], "min": {"cores": 2, "memory_mb": 2048, "disk_gb": 20},
     "fields": [
         {"env": "APP_PANEL_URL", "label": "Adresse des Pterodactyl Panels", "kind": "url", "required": False,
          "hint": "optional – für automatische Verbindung (Auto-Deploy)"},
         {"env": "APP_TOKEN", "label": "Auto-Deploy-Token", "kind": "token", "required": False,
          "hint": "Panel → Admin → Nodes → Configuration → Generate Token"},
         {"env": "APP_NODE_ID", "label": "Node-ID im Panel", "kind": "number", "required": False},
     ]},
    {"id": "mailcow", "name": "mailcow", "category": "E-Mail",
     "description": "Kompletter Mailserver (Postfix, Dovecot, SOGo-Webmail, Spam- und Virenschutz) auf Docker-Basis.",
     "types": ["kvm"], "min": {"cores": 2, "memory_mb": 6144, "disk_gb": 30},
     "fields": [
         {"env": "APP_DOMAIN", "label": "Hostname des Mailservers (FQDN)", "kind": "domain", "required": True,
          "hostname": True, "hint": "z. B. mail.example.de – muss per DNS auf den Server zeigen"},
         {"env": "APP_TZ", "label": "Zeitzone", "kind": "tz", "required": True, "default": "Europe/Berlin"},
     ]},
    {"id": "wordpress", "name": "WordPress", "category": "Web",
     "description": "WordPress mit nginx, MariaDB und PHP – fertig für den Einrichtungsassistenten.",
     "types": ["kvm", "lxc"], "min": {"cores": 1, "memory_mb": 1024, "disk_gb": 8},
     "fields": [
         {"env": "APP_DOMAIN", "label": "Domain", "kind": "domain", "required": False, "hostname": True,
          "hint": "optional – sonst über die IP erreichbar"},
         {"env": "APP_EMAIL", "label": "E-Mail für Let's Encrypt", "kind": "email", "required": False},
         {"env": "APP_SSL", "label": "HTTPS mit Let's Encrypt (Domain muss auf den Server zeigen)", "kind": "bool", "default": "0"},
     ]},
    {"id": "nextcloud-aio", "name": "Nextcloud (All-in-One)", "category": "Cloud",
     "description": "Offizielle Nextcloud-Komplettinstallation mit Office, Talk und Backups (Docker).",
     "types": ["kvm"], "min": {"cores": 2, "memory_mb": 4096, "disk_gb": 30},
     "fields": [{"env": "APP_DOMAIN", "label": "Domain", "kind": "domain", "required": False, "hostname": True}]},
    {"id": "coolify", "name": "Coolify", "category": "Hosting",
     "description": "Selbst gehostete Alternative zu Heroku/Vercel: Apps, Datenbanken und Websites per Git deployen.",
     "types": ["kvm"], "min": {"cores": 2, "memory_mb": 2048, "disk_gb": 30}, "fields": []},
    {"id": "portainer", "name": "Portainer", "category": "Docker",
     "description": "Docker mit grafischer Verwaltung (Portainer CE).",
     "types": ["kvm"], "min": {"cores": 1, "memory_mb": 1024, "disk_gb": 10}, "fields": []},
    {"id": "nginx-proxy-manager", "name": "Nginx Proxy Manager", "category": "Web",
     "description": "Reverse-Proxy mit Oberfläche und automatischen Let's-Encrypt-Zertifikaten (Docker).",
     "types": ["kvm"], "min": {"cores": 1, "memory_mb": 1024, "disk_gb": 10}, "fields": []},
    {"id": "uptime-kuma", "name": "Uptime Kuma", "category": "Monitoring",
     "description": "Überwachung von Websites und Diensten mit Statusseiten und Benachrichtigungen (Docker).",
     "types": ["kvm"], "min": {"cores": 1, "memory_mb": 1024, "disk_gb": 8}, "fields": []},
    {"id": "docker", "name": "Docker", "category": "Docker",
     "description": "Docker Engine und Docker Compose, sonst nichts.",
     "types": ["kvm"], "min": {"cores": 1, "memory_mb": 1024, "disk_gb": 8}, "fields": []},
    {"id": "nginx", "name": "nginx Webserver", "category": "Web",
     "description": "Schlanker Webserver für statische Seiten oder als Grundlage für eigene Projekte.",
     "types": ["kvm", "lxc"], "min": {"cores": 1, "memory_mb": 256, "disk_gb": 2},
     "fields": [{"env": "APP_DOMAIN", "label": "Domain", "kind": "domain", "required": False, "hostname": True}]},
]
BY_ID = {a["id"]: a for a in CATALOG}

router = APIRouter(prefix="/api/apps", tags=["apps"])


@router.get("")
def list_apps(user: User = Depends(current_user)):
    return CATALOG


def get(app_id: str | None) -> dict | None:
    if not app_id:
        return None
    app = BY_ID.get(app_id)
    if not app:
        raise HTTPException(400, "Unbekannte Anwendung")
    return app


def validate(app: dict, params: dict, gtype: str, template: Template | None, cores: int, memory_mb: int,
             disk_gb: int, hostname: str) -> dict:
    """Prüft Servertyp, System, Ressourcen und Eingaben. Gibt die bereinigten Werte zurück."""
    if gtype not in app["types"]:
        raise HTTPException(400, f"{app['name']} braucht eine virtuelle Maschine (VM), keinen Container"
                                 if app["types"] == ["kvm"] else f"{app['name']} ist hier nicht verfügbar")
    if template is None or template.os_family not in LINUX:
        raise HTTPException(400, f"{app['name']} braucht Debian oder Ubuntu als Betriebssystem")
    if template.source == "iso":
        raise HTTPException(400, "Anwendungen lassen sich nur mit Cloud-Images oder Containern installieren, nicht per ISO")
    need = app["min"]
    short = [f"{label} {need[k]}{unit}" for k, label, unit, have in
             (("cores", "CPU-Kerne:", "", cores), ("memory_mb", "RAM:", " MB", memory_mb), ("disk_gb", "Speicher:", " GB", disk_gb))
             if have < need[k]]
    if short:
        raise HTTPException(400, f"{app['name']} braucht mindestens " + ", ".join(short))
    clean = {}
    for f in app["fields"]:
        raw = params.get(f["env"])
        if raw is None or str(raw).strip() == "":
            raw = f.get("default", "")
            if not raw and f.get("hostname") and "." in hostname:
                raw = hostname  # Hostname des Servers als Domain vorschlagen
        value = str(raw).strip()
        if f["kind"] == "bool":
            value = "1" if value in ("1", "true", "True", "on") else "0"
        if not value:
            if f.get("required"):
                raise HTTPException(400, f"Bitte „{f['label']}“ angeben")
            continue
        rule, hint = FIELD_RULES[f["kind"]]
        if not re.match(rule, value):
            raise HTTPException(400, f"„{f['label']}“: {hint}")
        clean[f["env"]] = value
    return clean


def build_script(guest: Guest) -> str | None:
    """Fertiges Installationsskript für den Server (None = keine Anwendung)."""
    if not guest.app_id:
        return None
    app = BY_ID.get(guest.app_id)
    if not app:
        return None
    params = json.loads(guest.app_params or "{}")
    env = {"APP_NAME": app["name"], "GUEST_HOSTNAME": guest.hostname, **params}
    exports = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items() if re.match(r"^[A-Z_]+$", k))
    common = (SCRIPTS / "_common.sh").read_text(encoding="utf-8")
    body = (SCRIPTS / f"{app['id']}.sh").read_text(encoding="utf-8")
    return f"#!/bin/bash\n# PomBot VE – Installation von {app['name']}\n{exports}\n{common}\n{body}"
