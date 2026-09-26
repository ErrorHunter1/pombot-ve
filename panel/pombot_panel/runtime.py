"""Einstellungen, die im Adminbereich geändert werden können.

Werte stehen in der Tabelle `settings` (Schlüssel `cfg.<name>`) und überschreiben die Vorgaben aus
/etc/pombot/panel.env. Port, Adresse und Zertifikat des Panels stehen zusätzlich in
<data_dir>/runtime.env, weil systemd sie beim Start braucht.
"""
import asyncio
import logging
import os
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .config import settings
from .models import Setting

log = logging.getLogger("pombot.runtime")

# Name -> Typ. "secret" wird nie im Klartext an den Browser zurückgegeben.
EDITABLE: dict[str, str] = {
    "registration": "choice:open,approval,closed",
    "discord_client_id": "str",
    "discord_client_secret": "secret",
    "discord_guild_id": "str",
    "discord_admin_ids": "set",
    "default_max_guests": "int",
    "default_max_cores": "int",
    "default_max_memory_mb": "int",
    "default_max_disk_gb": "int",
    "default_max_ips": "int",
    "default_dns": "str",
    "antispoof": "bool",
    "max_auto_backups": "int",
    "update_check_hours": "int",
    "api_enabled": "bool",
    "cloudflare_token": "secret",
    "acme_email": "str",
    "panel_domain": "str",
    "acme_enabled": "bool",
    "acme_staging": "bool",
}


def _parse(kind: str, raw: str):
    if kind == "int":
        return int(raw)
    if kind == "bool":
        return raw in ("1", "true", "True")
    if kind == "set":
        return {x.strip() for x in raw.split(",") if x.strip()}
    return raw


def _serialize(kind: str, value) -> str:
    if kind == "bool":
        return "1" if value else "0"
    if kind == "set":
        return ",".join(sorted(value))
    return str(value)


def load(db: Session) -> None:
    """Überschreibt die Werte aus panel.env mit den im Adminbereich gespeicherten."""
    for row in db.query(Setting).filter(Setting.key.like("cfg.%")):
        name = row.key[4:]
        kind = EDITABLE.get(name)
        if kind:
            try:
                setattr(settings, name, _parse(kind, row.value))
            except ValueError:
                log.warning("Ungültiger gespeicherter Wert für %s", name)


def view() -> dict:
    data = {}
    for name, kind in EDITABLE.items():
        value = getattr(settings, name, None)
        if kind == "secret":
            data[name] = ""
            data[f"{name}_set"] = bool(value)
        elif kind == "set":
            data[name] = ", ".join(sorted(value or []))
        else:
            data[name] = value
    return data


def save(db: Session, updates: dict) -> list[str]:
    changed = []
    for name, value in updates.items():
        kind = EDITABLE.get(name)
        if not kind or value is None:
            continue
        if kind == "secret" and value == "":
            continue  # leeres Feld = unverändert lassen
        if kind.startswith("choice:"):
            if value not in kind[7:].split(","):
                raise HTTPException(400, f"Ungültiger Wert für {name}")
            parsed = value
        elif kind == "int":
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{name} muss eine Zahl sein")
            if parsed < 0:
                raise HTTPException(400, f"{name} darf nicht negativ sein")
        elif kind == "bool":
            parsed = bool(value)
        elif kind == "set":
            parsed = {x.strip() for x in str(value).split(",") if x.strip()}
        else:
            parsed = str(value).strip()
        stored = _serialize("str" if kind.startswith("choice:") else kind, parsed)
        row = db.get(Setting, f"cfg.{name}")
        if row:
            row.value = stored
        else:
            db.add(Setting(key=f"cfg.{name}", value=stored))
        setattr(settings, name, parsed)
        changed.append(name)
    return changed


# ---------------------------------------------------------------- Port / Adresse / Zertifikat

def runtime_env_path() -> Path:
    return Path(settings.data_dir) / "runtime.env"


def tls_dir() -> Path:
    return Path(settings.data_dir) / "tls"


def write_runtime_env(base_url: str, port: int, cert: Path | None, key: Path | None) -> None:
    lines = [f"POMBOT_BASE_URL={base_url}", f"POMBOT_PORT={port}"]
    if cert and key:
        lines += [f"POMBOT_TLS_CERT={cert}", f"POMBOT_TLS_KEY={key}"]
    path = runtime_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Von PomBot VE verwaltet (Adminbereich → Domain & HTTPS)\n" + "\n".join(lines) + "\n")


def reset_runtime_env() -> None:
    runtime_env_path().unlink(missing_ok=True)


def restart_supported() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))  # läuft unter systemd (Restart=always)


def schedule_restart(loop: asyncio.AbstractEventLoop | None, delay: float = 3) -> bool:
    """Beendet den Prozess nach kurzer Pause; systemd startet das Panel mit der neuen Konfiguration neu."""
    if not restart_supported() or loop is None:
        return False
    loop.call_soon_threadsafe(loop.call_later, delay, os._exit, 0)
    return True
