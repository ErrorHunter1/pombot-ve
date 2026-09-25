"""Täglicher Heartbeat an https://vm.errorhunter.it/heartbeats.

Gesendet werden nur technische Daten: Versionen, eine zufällige Installations-ID sowie Kennzahlen zu
Nodes und VMs. Die Meldung enthält keine IP-Adressen, Hostnamen, Server-Namen, Benutzer, E-Mails oder
Passwörter (der Empfänger sieht – wie bei jeder Verbindung – die öffentliche IP des Panels).

Abschalten: POMBOT_HEARTBEAT=0 in /etc/pombot/panel.env, dann `systemctl restart pombot-panel`.
Anderes Ziel: POMBOT_HEARTBEAT_URL=https://…
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import Guest, Node, Setting

log = logging.getLogger("pombot.heartbeat")

URL = os.environ.get("POMBOT_HEARTBEAT_URL", "https://vm.errorhunter.it/heartbeats")
ENABLED = os.environ.get("POMBOT_HEARTBEAT", "1") == "1"
INTERVAL = 24 * 3600
FIRST_DELAY = 60  # nach dem Start kurz warten, bis die Nodes einmal abgefragt wurden


def installation_id() -> str:
    """Zufällige, dauerhafte ID dieser Panel-Installation (wird beim ersten Heartbeat erzeugt)."""
    with session_scope() as db:
        row = db.get(Setting, "installation_id")
        if not row:
            row = Setting(key="installation_id", value=uuid.uuid4().hex)
            db.add(row)
        return row.value


def build_payload() -> dict:
    with session_scope() as db:
        nodes = [{
            "node": n.id,
            "status": n.status,
            "agent_version": n.info.get("agent_version"),
            "os": n.info.get("os"),
            "kernel": n.info.get("kernel"),
            "virtualization": n.info.get("virtualization"),
            "kvm": n.info.get("kvm"),
            "cpu_cores": n.info.get("cpu_cores"),
            "memory_total": n.info.get("memory_total"),
            "lxc": n.info.get("lxc"),
            "libvirt": n.info.get("libvirt"),
        } for n in db.scalars(select(Node).order_by(Node.id))]
        vms = [{
            "vmid": g.vmid,
            "node": g.node_id,
            "type": g.type,
            "os": g.template.name if g.template else ("ISO" if g.iso_file else None),
            "cores": g.cores,
            "memory_mb": g.memory_mb,
            "disk_gb": g.disk_gb,
            "status": g.status,
            "power": g.power,
            "network": ("routed" if any(ip.pool.mode == "routed" for ip in g.ips) else "bridged") if g.ips else "dhcp",
            "created_at": g.created_at.replace(tzinfo=timezone.utc).isoformat(),
        } for g in db.scalars(select(Guest).order_by(Guest.vmid))]
    return {
        "installation_id": installation_id(),
        "panel_version": settings.version,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "nodes": len(nodes),
            "nodes_online": sum(n["status"] == "online" for n in nodes),
            "vms": sum(v["type"] == "kvm" for v in vms),
            "containers": sum(v["type"] == "lxc" for v in vms),
            "running": sum(v["power"] == "running" for v in vms),
        },
        "nodes": nodes,
        "vms": vms,
    }


async def send_once() -> bool:
    payload = await asyncio.to_thread(build_payload)
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": f"pombot-ve/{settings.version}"}) as client:
            resp = await client.post(URL, json=payload)
        log.info("Heartbeat an %s: HTTP %s", URL, resp.status_code)
        return resp.status_code < 400
    except httpx.HTTPError as exc:
        log.warning("Heartbeat an %s fehlgeschlagen: %s", URL, exc)
        return False


async def heartbeat_loop() -> None:
    if not ENABLED:
        log.info("Heartbeat ist deaktiviert (POMBOT_HEARTBEAT=0)")
        return
    await asyncio.sleep(FIRST_DELAY)
    while True:
        try:
            await send_once()
        except Exception:  # noqa: BLE001 – der Heartbeat darf das Panel nie stören
            log.exception("Heartbeat-Fehler")
        await asyncio.sleep(INTERVAL)
