"""Gemeinsamer Speicher und Hochverfügbarkeit (HA).

- Speicher (NFS, SMB oder ein vorhandener Mount wie CephFS) werden im Adminbereich angelegt und auf
  allen Nodes eingehängt (PUT /storage). Server können beim Anlegen darauf gelegt werden.
- HA-Server: Ist ihr Node länger als HA_GRACE Sekunden nicht erreichbar, übernimmt ein anderer Node mit
  demselben Speicher den Server und startet ihn. Die eigentliche Absicherung gegen doppelt laufende Server
  machen die Agents über Leases auf dem Speicher (siehe agent/pombot_agent/ha.py): der Ziel-Node übernimmt
  nur, wenn die Lease des alten Nodes abgelaufen ist.
- Kommt der alte Node zurück, wird der Server dort nur abgemeldet (die Daten liegen ja auf dem Speicher).
"""
import asyncio
import json
import logging
import re
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ipam
from .agent_client import AgentClient, AgentError
from .db import get_db, session_scope
from .models import Guest, Node, SharedStorage, User, now
from .perms import require
from .security import audit, client_ip, current_user

log = logging.getLogger("pombot.cluster")

FIELDS = {"nfs": ["server", "export", "options"], "smb": ["share", "user", "password", "domain", "version"],
          "path": ["path"]}
SECRETS = {"password"}
LABELS = {"nfs": "NFS", "smb": "SMB", "path": "Lokaler Mount (z. B. CephFS)"}
HA_GRACE = 120  # Sekunden ohne Kontakt, bevor ein Node als ausgefallen gilt
RETRY_AFTER = 300  # nach fehlgeschlagener Übernahme erst so viel später erneut versuchen
BUSY = {"creating", "deleting", "busy"}

STATUS: dict[int, dict[str, dict]] = {}  # node_id -> storage_id -> {"mounted", "free", "error", …}
_WAS_RUNNING: set[int] = set()  # HA-Server, die liefen, als ihr Node ausfiel
_OFFLINE_SEEN: set[int] = set()  # Nodes, deren Ausfall dieses Panel selbst beobachtet hat
_ATTEMPT: dict[int, float] = {}
_ACTIVE: set[int] = set()
_JOBS: set = set()

router = APIRouter(prefix="/api/admin/storages", tags=["storages"])
user_router = APIRouter(prefix="/api/storages", tags=["storages"])


# ---------------------------------------------------------------- Hilfsfunktionen

def payload(s: SharedStorage) -> dict:
    return {"id": str(s.id), "type": s.type, **s.config}


def mounted_on(node_id: int, storage_id: int | None) -> bool:
    return bool(storage_id) and bool(STATUS.get(node_id, {}).get(str(storage_id), {}).get("mounted"))


def public(s: SharedStorage, db: Session) -> dict:
    cfg = s.config
    shown = {k: ("" if k in SECRETS else v) for k, v in cfg.items()}
    if s.type == "smb":
        shown["password_set"] = bool(cfg.get("password"))
    where = {"nfs": f"{cfg.get('server')}:{cfg.get('export')}", "smb": cfg.get("share"), "path": cfg.get("path")}[s.type]
    nodes = []
    for node in db.scalars(select(Node).order_by(Node.name)):
        st = STATUS.get(node.id, {}).get(str(s.id))
        nodes.append({"node": node.name, "online": node.status == "online", "mounted": bool(st and st.get("mounted")),
                      "free": (st or {}).get("free"), "total": (st or {}).get("total"), "error": (st or {}).get("error")})
    used = len(list(db.scalars(select(Guest.id).where(Guest.storage_id == s.id))))
    return {"id": s.id, "name": s.name, "type": s.type, "type_label": LABELS[s.type], "where": where,
            "user_visible": s.user_visible, "config": shown, "nodes": nodes, "guests": used}


def visible(db: Session, user: User) -> list[SharedStorage]:
    return [s for s in db.scalars(select(SharedStorage).order_by(SharedStorage.name)) if user.can("storage.manage") or s.user_visible]


def get_visible(db: Session, user: User, storage_id) -> SharedStorage | None:
    if storage_id in (None, 0, "", "local"):
        return None
    s = db.get(SharedStorage, int(storage_id))
    if not s or (not user.can("storage.manage") and not s.user_visible):
        raise HTTPException(404, "Speicher nicht gefunden")
    return s


# ---------------------------------------------------------------- Abgleich mit den Nodes

def _node_payload(db: Session, node_id: int) -> tuple[list[dict], list[str]]:
    storages = [payload(s) for s in db.scalars(select(SharedStorage))]
    ha = [g.agent_name for g in db.scalars(select(Guest).where(Guest.node_id == node_id, Guest.ha.is_(True)))]
    return storages, ha


async def sync_node(node_id: int, client: AgentClient) -> None:
    """Speicherliste und HA-Server an einen Node übertragen und den Speicherzustand merken."""
    with session_scope() as db:
        storages, ha = _node_payload(db, node_id)
    result = await client.arequest("PUT", "/storage", {"storages": storages}, timeout=120)
    STATUS[node_id] = {str(r["id"]): r for r in result}
    await client.arequest("PUT", "/ha", {"guests": ha}, timeout=60)


def sync_node_now(node_id: int) -> None:
    """Synchron (aus Endpunkten im Threadpool)."""
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node or node.status != "online":
            return
        client = AgentClient.for_node(node)
        storages, ha = _node_payload(db, node_id)
    result = client.request("PUT", "/storage", {"storages": storages}, timeout=120)
    STATUS[node_id] = {str(r["id"]): r for r in result}
    client.request("PUT", "/ha", {"guests": ha}, timeout=60)


def sync_all() -> list[str]:
    errors = []
    with session_scope() as db:
        nodes = [(n.id, n.name) for n in db.scalars(select(Node).where(Node.status == "online"))]
    for node_id, name in nodes:
        try:
            sync_node_now(node_id)
        except AgentError as exc:
            errors.append(f"{name}: {exc}")
    return errors


def node_went_offline(db: Session, node: Node) -> None:
    """Vom Poller beim Übergang online → offline aufgerufen (vor dem Zurücksetzen von power)."""
    _OFFLINE_SEEN.add(node.id)
    STATUS.pop(node.id, None)
    for g in node.guests:
        if g.ha and g.power == "running":
            _WAS_RUNNING.add(g.id)


async def cleanup_stale(node_id: int, client: AgentClient, present: dict) -> None:
    """Server, die auf diesem Node noch registriert sind, laut Panel aber woanders laufen (nach HA-Übernahme
    oder Umzug über gemeinsamen Speicher): hier abmelden. Die Daten auf dem Speicher bleiben unberührt."""
    stale = []
    with session_scope() as db:
        for g in db.scalars(select(Guest).where(Guest.storage_id.is_not(None), Guest.node_id != node_id)):
            if g.agent_name in present and g.status not in BUSY and g.id not in _ACTIVE:
                stale.append((g.agent_name, present[g.agent_name].get("state")))
    for name, state in stale:
        if state == "running":
            log.error("%s läuft auf Node %s, gehört laut Panel aber zu einem anderen Node – bitte prüfen", name, node_id)
            continue
        try:
            await client.arequest("POST", f"/guests/{name}/release?stale=true", timeout=60)
            log.info("%s auf Node %s abgemeldet (läuft inzwischen auf einem anderen Node)", name, node_id)
        except AgentError as exc:
            log.warning("Abmelden von %s auf Node %s fehlgeschlagen: %s", name, node_id, exc)


async def restart_returned(node_id: int, present: dict) -> None:
    """HA-Server, die liefen, als ihr Node ausfiel, nach der Rückkehr des Nodes wieder starten
    (HA-Server starten beim Booten nicht von selbst)."""
    _OFFLINE_SEEN.discard(node_id)
    with session_scope() as db:
        todo = [(g.id, g.agent_name) for g in db.scalars(select(Guest).where(Guest.node_id == node_id, Guest.ha.is_(True)))
                if g.id in _WAS_RUNNING and g.status not in BUSY and present.get(g.agent_name, {}).get("state") == "stopped"]
        node = db.get(Node, node_id)
        client = AgentClient.for_node(node)
    for gid, name in todo:
        _WAS_RUNNING.discard(gid)
        try:
            await client.arequest("POST", f"/guests/{name}/action", {"action": "start"}, timeout=150)
            log.info("HA: %s nach Rückkehr von Node %s wieder gestartet", name, node_id)
        except AgentError as exc:
            log.warning("HA: Start von %s fehlgeschlagen: %s", name, exc)


# ---------------------------------------------------------------- HA-Übernahme

def _pick_target(db: Session, g: Guest) -> Node | None:
    candidates = []
    for node in db.scalars(select(Node).where(Node.status == "online", Node.id != g.node_id)):
        from .ops import node_free_memory_mb, node_supports, pool_fits_node
        if not node_supports(node, g.type) or not mounted_on(node.id, g.storage_id):
            continue
        if not all(pool_fits_node(ip.pool, node) for ip in g.ips):
            continue
        free = node_free_memory_mb(node)
        if free < g.memory_mb:
            continue
        candidates.append((node.enabled, free, node))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return candidates[0][2] if candidates else None


def network_for(g: Guest, node: Node) -> dict:
    from .ops import default_bridge
    ips = sorted(g.ips, key=lambda r: ipam.version_of(r.pool))
    first = ips[0].pool if ips else None
    routed = bool(first and ipam.is_routed(first))
    return {"mac": g.mac, "hostname": g.hostname, "routed": routed,
            "bridge": ipam.ROUTED_BRIDGE if routed else first.bridge if first else default_bridge(node),
            "ips": [ipam.ip_spec(r, node) for r in ips], "dns": ipam.pool_dns(first)}


async def ha_check() -> None:
    """Vom Poller nach jeder Runde: HA-Server auf ausgefallenen Nodes übernehmen."""
    from .tasks import create_task, run_task
    limit = now() - timedelta(seconds=HA_GRACE)
    starts = []
    with session_scope() as db:
        for node in db.scalars(select(Node).where(Node.status == "offline")):
            if node.last_seen and node.last_seen > limit:
                continue
            for g in node.guests:
                if not g.ha or not g.storage_id or g.status in BUSY or g.id in _ACTIVE:
                    continue
                if time.time() - _ATTEMPT.get(g.id, 0) < RETRY_AFTER:
                    continue
                _ATTEMPT[g.id] = time.time()
                target = _pick_target(db, g)
                if not target:
                    log.warning("HA: kein passender Node für %s (#%s) – Speicher eingehängt, Pools, RAM?", g.name, g.vmid)
                    continue
                start = g.id in _WAS_RUNNING or node.id not in _OFFLINE_SEEN
                task = create_task(db, None, "ha-failover", f"{g.name} (#{g.vmid}): {node.name} → {target.name}",
                                   target.id, g.id)
                audit(db, None, "ha-failover", f"#{g.vmid} {node.name} -> {target.name}")
                starts.append((task.id, g.id, target.id, network_for(g, target), start))
    for task_id, gid, target_id, network, start in starts:
        _ACTIVE.add(gid)
        job = asyncio.create_task(run_task(task_id, _failover(task_id, gid, target_id, network, start)))
        _JOBS.add(job)
        job.add_done_callback(_JOBS.discard)


async def _failover(task_id: int, guest_id: int, target_id: int, network: dict, start: bool) -> None:
    from .ops import _set_guest
    from .tasks import agent_job, task_log
    try:
        with session_scope() as db:
            g, target = db.get(Guest, guest_id), db.get(Node, target_id)
            name, gtype, storage_id, old = g.agent_name, g.type, str(g.storage_id), g.node.name
            dst = AgentClient.for_node(target)
            g.status = "busy"
        task_log(task_id, f"Node {old} seit über {HA_GRACE} s nicht erreichbar – übernehme {name} auf {target.name}.")
        await dst.arequest("POST", f"/guests/{name}/adopt", {"type": gtype, "storage_id": storage_id, "ha": True},
                           timeout=120)
        task_log(task_id, "Server vom gemeinsamen Speicher registriert. Richte Netzwerk ein …")
        try:
            await agent_job(task_id, dst, "POST", f"/guests/{name}/network", network)
        except Exception:
            await dst.arequest("POST", f"/guests/{name}/release", timeout=60)
            raise
        _set_guest(guest_id, node_id=target_id, power="stopped", status="ready", error=None)
        from .routers.extras import push_firewall
        try:
            await asyncio.to_thread(push_firewall, guest_id)
        except Exception as exc:  # noqa: BLE001
            task_log(task_id, f"WARNUNG: Firewall nicht gesetzt: {exc}")
        if start:
            state = (await dst.arequest("POST", f"/guests/{name}/action", {"action": "start"}, timeout=150))["state"]
            _set_guest(guest_id, power=state)
            task_log(task_id, f"Server läuft jetzt auf {target.name}.")
        else:
            task_log(task_id, "Server war vor dem Ausfall gestoppt – bleibt gestoppt.")
        _WAS_RUNNING.discard(guest_id)
        _ATTEMPT.pop(guest_id, None)
    finally:
        _ACTIVE.discard(guest_id)
        _set_guest(guest_id, status="ready")


# ---------------------------------------------------------------- Admin-API

class StorageBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern="^(nfs|smb|path)$")
    config: dict
    user_visible: bool = False


def _clean(body: StorageBody, old: dict | None = None) -> dict:
    old = old or {}
    cfg = {}
    for key in FIELDS[body.type]:
        value = body.config.get(key)
        if key in SECRETS and not value:
            value = old.get(key, "")
        cfg[key] = str(value or "").strip()
    if body.type == "nfs":
        if not re.match(r"^[A-Za-z0-9.\-:\[\]]{1,253}$", cfg["server"]) or not cfg["export"].startswith("/"):
            raise HTTPException(400, "NFS: Server und Export (beginnt mit /) angeben")
        if cfg["options"] and not re.match(r"^[A-Za-z0-9=,._\-]{1,200}$", cfg["options"]):
            raise HTTPException(400, "Ungültige NFS-Optionen")
    elif body.type == "smb":
        if not re.match(r"^//[A-Za-z0-9.\-]+/[^\s/][^\s]*$", cfg["share"]):
            raise HTTPException(400, "SMB-Freigabe als //server/freigabe angeben")
    else:
        if not cfg["path"].startswith("/") or ".." in cfg["path"].split("/") or cfg["path"].rstrip("/") in ("", "/var", "/var/lib"):
            raise HTTPException(400, "Absoluten Pfad eines eingehängten Speichers angeben, z. B. /mnt/cephfs")
    return {k: v for k, v in cfg.items() if v != ""}


@router.get("")
def list_storages(user: User = Depends(require("storage.manage")), db: Session = Depends(get_db)):
    return [public(s, db) for s in db.scalars(select(SharedStorage).order_by(SharedStorage.name))]


@router.post("")
def create_storage(body: StorageBody, request: Request, user: User = Depends(require("storage.manage")),
                   db: Session = Depends(get_db)):
    if db.scalar(select(SharedStorage).where(SharedStorage.name == body.name.strip())):
        raise HTTPException(400, "Name bereits vergeben")
    s = SharedStorage(name=body.name.strip(), type=body.type, config_json=json.dumps(_clean(body)),
                      user_visible=body.user_visible)
    db.add(s)
    audit(db, user, "storage-create", f"{s.name} ({s.type})", client_ip(request))
    db.commit()
    errors = sync_all()
    db.refresh(s)
    return {**public(s, db), "sync_errors": errors}


@router.put("/{storage_id}")
def update_storage(storage_id: int, body: StorageBody, request: Request, user: User = Depends(require("storage.manage")),
                   db: Session = Depends(get_db)):
    s = db.get(SharedStorage, storage_id)
    if not s:
        raise HTTPException(404, "Speicher nicht gefunden")
    in_use = db.scalar(select(Guest.id).where(Guest.storage_id == s.id))
    cfg = _clean(body, s.config if body.type == s.type else {})
    if in_use and (body.type != s.type or {k: v for k, v in cfg.items() if k not in SECRETS | {"options", "version"}}
                   != {k: v for k, v in s.config.items() if k not in SECRETS | {"options", "version"}}):
        raise HTTPException(400, "Der Speicher wird von Servern genutzt – Ziel kann nicht geändert werden")
    s.name, s.type, s.config_json, s.user_visible = body.name.strip(), body.type, json.dumps(cfg), body.user_visible
    audit(db, user, "storage-update", s.name, client_ip(request))
    db.commit()
    errors = sync_all()
    return {**public(s, db), "sync_errors": errors}


@router.delete("/{storage_id}")
def delete_storage(storage_id: int, request: Request, user: User = Depends(require("storage.manage")),
                   db: Session = Depends(get_db)):
    s = db.get(SharedStorage, storage_id)
    if not s:
        raise HTTPException(404, "Speicher nicht gefunden")
    if db.scalar(select(Guest.id).where(Guest.storage_id == s.id)):
        raise HTTPException(400, "Auf diesem Speicher liegen noch Server")
    audit(db, user, "storage-delete", s.name, client_ip(request))
    db.delete(s)
    db.commit()
    sync_all()
    return {"ok": True}


@router.post("/sync")
def sync_storages(user: User = Depends(require("storage.manage"))):
    """Speicher jetzt auf allen Nodes einhängen und Zustand abfragen."""
    return {"errors": sync_all()}


@user_router.get("")
def storages_for_create(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Speicher, die beim Anlegen gewählt werden können (mit den Nodes, auf denen sie eingehängt sind)."""
    nodes = list(db.scalars(select(Node)))
    return [{"id": s.id, "name": s.name, "type_label": LABELS[s.type],
             "nodes": [n.id for n in nodes if mounted_on(n.id, s.id)]} for s in visible(db, user)]
