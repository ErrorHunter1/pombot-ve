"""Firewall und zeitgesteuerte Backups pro Server."""
import ipaddress
import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent_client import AgentClient, AgentError
from ..config import settings
from ..db import get_db, session_scope
from ..models import BackupSchedule, FirewallConfig, Guest, User, now
from .. import backup_targets
from ..scheduler import next_run
from ..security import audit, client_ip, current_user, guest_for

router = APIRouter(prefix="/api/guests", tags=["extras"])
PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$")
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


# ---------------------------------------------------------------- Firewall

def firewall_payload(g: Guest, fw: FirewallConfig | None) -> dict:
    """Konfiguration, wie sie der Agent erwartet."""
    return {
        "enabled": bool(fw and fw.enabled),
        "policy_in": fw.policy_in if fw else "accept",
        "policy_out": fw.policy_out if fw else "accept",
        "antispoof": fw.antispoof if fw else settings.antispoof,
        "rules": fw.rules if fw else [],
        "mac": g.mac,
        "ips": [ip.address for ip in g.ips],
    }


def push_firewall(guest_id: int) -> None:
    """Überträgt die Firewall eines Servers auf seinen Node (synchron)."""
    with session_scope() as db:
        g = db.get(Guest, guest_id)
        if not g:
            return
        payload = firewall_payload(g, db.get(FirewallConfig, g.id))
        client, name = AgentClient.for_node(g.node), g.agent_name
    client.request("PUT", f"/guests/{name}/firewall", payload, timeout=30)


def fw_dict(g: Guest, fw: FirewallConfig | None) -> dict:
    data = firewall_payload(g, fw)
    data.pop("mac")
    if not fw:
        data["policy_in"] = "drop"  # sinnvolle Vorgabe, sobald der Benutzer die Firewall einschaltet
    data["updated_at"] = fw.updated_at.isoformat() + "Z" if fw else None
    return data


class Rule(BaseModel):
    direction: str = Field(pattern="^(in|out)$")
    action: str = Field(pattern="^(accept|drop)$")
    protocol: str = Field(pattern="^(tcp|udp|icmp|any)$")
    port: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=64)
    comment: str = Field(default="", max_length=100)
    enabled: bool = True


class FirewallBody(BaseModel):
    enabled: bool
    policy_in: str = Field(pattern="^(accept|drop)$")
    policy_out: str = Field(pattern="^(accept|drop)$")
    antispoof: bool | None = None
    rules: list[Rule] = Field(default_factory=list, max_length=100)


def _check_rule(i: int, r: Rule) -> dict:
    label = f"Regel {i + 1}"
    port = r.port.replace(" ", "")
    if port:
        if r.protocol not in ("tcp", "udp"):
            raise HTTPException(400, f"{label}: Ports gibt es nur bei TCP und UDP")
        if not PORT_RE.match(port):
            raise HTTPException(400, f"{label}: Ports als 22, 80,443 oder 1000-2000 angeben")
        for part in port.split(","):
            bounds = [int(x) for x in part.split("-")]
            if any(not 1 <= p <= 65535 for p in bounds) or (len(bounds) == 2 and bounds[0] > bounds[1]):
                raise HTTPException(400, f"{label}: ungültiger Port {part}")
    source = r.source.strip()
    if source:
        try:
            source = str(ipaddress.ip_network(source, strict=False))
        except ValueError:
            raise HTTPException(400, f"{label}: ungültige Adresse {source} (Beispiel: 203.0.113.0/24)")
    return {**r.model_dump(), "port": port, "source": source}


@router.get("/{guest_id}/firewall")
def get_firewall(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    return fw_dict(g, db.get(FirewallConfig, g.id))


@router.put("/{guest_id}/firewall")
def put_firewall(guest_id: int, body: FirewallBody, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if g.status in ("creating", "deleting"):
        raise HTTPException(409, "Der Server ist gerade beschäftigt")
    rules = [_check_rule(i, r) for i, r in enumerate(body.rules)]
    fw = db.get(FirewallConfig, g.id)
    if not fw:
        fw = FirewallConfig(guest_id=g.id, antispoof=settings.antispoof)
        db.add(fw)
    fw.enabled, fw.policy_in, fw.policy_out = body.enabled, body.policy_in, body.policy_out
    if body.antispoof is not None and user.is_admin:
        fw.antispoof = body.antispoof
    fw.rules_json = json.dumps(rules)
    fw.updated_at = now()
    try:
        _agent_put(g, fw)
    except AgentError as exc:
        db.rollback()
        raise HTTPException(400, f"Firewall konnte nicht angewendet werden: {exc}")
    audit(db, user, "firewall-update",
          f"#{g.vmid} {'an' if fw.enabled else 'aus'}, {len(rules)} Regeln, ein={fw.policy_in}", client_ip(request))
    db.commit()
    return fw_dict(g, fw)


def _agent_put(g: Guest, fw: FirewallConfig) -> None:
    AgentClient.for_node(g.node).request("PUT", f"/guests/{g.agent_name}/firewall", firewall_payload(g, fw),
                                         timeout=30)


# ---------------------------------------------------------------- Zeitgesteuerte Backups

def schedule_dict(s: BackupSchedule | None) -> dict:
    if not s:
        return {"enabled": False, "frequency": "daily", "weekday": 6, "hour": 3, "minute": 0, "keep": 7,
                "target_id": None, "keep_local": False,
                "last_run": None, "last_status": None, "next_run": None, "exists": False}
    nxt = next_run(s) if s.enabled else None
    return {"enabled": s.enabled, "frequency": s.frequency, "weekday": s.weekday, "hour": s.hour,
            "minute": s.minute, "keep": s.keep, "exists": True, "target_id": s.target_id, "keep_local": s.keep_local,
            "last_run": s.last_run.isoformat() if s.last_run else None, "last_status": s.last_status,
            "next_run": nxt.isoformat() if nxt else None}


class ScheduleBody(BaseModel):
    enabled: bool
    frequency: str = Field(pattern="^(daily|weekly)$")
    weekday: int = Field(default=6, ge=0, le=6)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    keep: int = Field(ge=1, le=60)
    target_id: int | None = None
    keep_local: bool = False


@router.get("/{guest_id}/backup-schedule")
def get_schedule(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    data = schedule_dict(db.get(BackupSchedule, g.id))
    data["max_keep"] = 60 if user.is_admin else settings.max_auto_backups
    data["targets"] = [{"id": t.id, "name": t.name, "type": t.type} for t in backup_targets.visible(db, user)]
    return data


@router.put("/{guest_id}/backup-schedule")
def put_schedule(guest_id: int, body: ScheduleBody, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if not user.is_admin and body.keep > settings.max_auto_backups:
        raise HTTPException(400, f"Es können maximal {settings.max_auto_backups} automatische Backups "
                                 "aufbewahrt werden")
    backup_targets.get_visible(db, user, body.target_id)  # prüft Berechtigung
    s = db.get(BackupSchedule, g.id)
    if not s:
        s = BackupSchedule(guest_id=g.id)
        db.add(s)
    changed_time = (s.frequency, s.weekday, s.hour, s.minute) != (body.frequency, body.weekday, body.hour, body.minute)
    for key, value in body.model_dump().items():
        setattr(s, key, value)
    if changed_time or s.last_run is None:
        # Erst ab dem nächsten regulären Termin sichern, nicht sofort nachholen
        s.last_run = datetime.now().replace(microsecond=0)
    when = f"täglich {body.hour:02d}:{body.minute:02d}" if body.frequency == "daily" else \
        f"{WEEKDAYS[body.weekday]}s {body.hour:02d}:{body.minute:02d}"
    audit(db, user, "backup-schedule", f"#{g.vmid} {'an' if body.enabled else 'aus'}, {when}, behalte {body.keep}",
          client_ip(request))
    db.commit()
    return schedule_dict(s)

