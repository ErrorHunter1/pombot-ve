"""Server (VMs und Container): anlegen, steuern, ändern, löschen, Snapshots, Backups, Konsole."""
import asyncio
import ipaddress
import re
from datetime import timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect as ws_connect

import json

from .. import apps, backup_targets, cluster, ipam, ops
from ..agent_client import AgentClient, AgentError
from ..db import SessionLocal, get_db, session_scope
from ..models import Guest, IPAddress, IPPool, Node, Template, User
from ..security import audit, client_ip, current_user, generate_password, guest_for
from ..tasks import create_task, run_task, submit

router = APIRouter(prefix="/api/guests", tags=["guests"])
HOSTNAME = r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,62})$"
USER_SNAPSHOT_LIMIT = 10
USER_BACKUP_LIMIT = 5


def guest_dict(g: Guest) -> dict:
    return {
        "id": g.id, "vmid": g.vmid, "name": g.name, "hostname": g.hostname, "type": g.type,
        "node_id": g.node_id, "node": g.node.name if g.node else None,
        "owner_id": g.owner_id, "owner": g.owner.username if g.owner else None,
        "template_id": g.template_id, "template": g.template.name if g.template else (f"ISO: {g.iso_file}" if g.iso_file else None),
        "iso_file": g.iso_file,
        "template_source": g.template.source if g.template else None,
        "cores": g.cores, "memory_mb": g.memory_mb, "disk_gb": g.disk_gb, "mac": g.mac,
        "status": g.status, "power": g.power, "error": g.error, "notes": g.notes,
        "created_at": g.created_at.isoformat() + "Z",
        "ips": [_ip_view(ip, g) for ip in g.ips],
        "storage_id": g.storage_id, "storage": g.storage.name if g.storage else None, "ha": g.ha,
        "app_id": g.app_id, "app": apps.BY_ID[g.app_id]["name"] if g.app_id in apps.BY_ID else None,
        "onboot": g.onboot, "protected": g.protected, "tags": [t for t in (g.tags or "").split(",") if t],
    }


def _ip_view(ip, g: Guest) -> dict:
    routed = ipam.is_routed(ip.pool)
    gateway = (g.node.info.get("main_ipv4") if g.node else None) if routed else ip.pool.gateway
    return {"address": ip.address, "pool": ip.pool.name, "gateway": gateway, "prefix": ipam.prefix_of(ip.pool),
            "version": ipam.version_of(ip.pool), "routed": routed}


def _agent(g: Guest) -> AgentClient:
    return AgentClient.for_node(g.node)


def _call(fn):
    try:
        return fn()
    except AgentError as exc:
        raise HTTPException(502, str(exc))


def _ensure_ready(g: Guest) -> None:
    if g.status in ("creating", "deleting", "busy"):
        raise HTTPException(409, "Der Server ist gerade mit einer anderen Aufgabe beschäftigt")


def _keys(raw: str | None) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip() and not line.startswith("#")]


# ---------------------------------------------------------------- Liste / Details

@router.get("")
def list_guests(user: User = Depends(current_user), db: Session = Depends(get_db), all: bool = False):
    q = select(Guest).order_by(Guest.vmid)
    if not (user.is_admin and all):
        q = q.where(Guest.owner_id == user.id)
    return [guest_dict(g) for g in db.scalars(q)]


@router.get("/{guest_id}")
def get_guest(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return guest_dict(guest_for(db, user, guest_id))


@router.get("/{guest_id}/status")
def guest_status(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if g.status == "creating":
        return {"state": "creating"}
    live = _call(lambda: _agent(g).request("GET", f"/guests/{g.agent_name}", timeout=20))
    if g.status not in ("deleting", "busy") and live.get("state") and live["state"] != g.power:
        g.power = live["state"]
        db.commit()
    return live


# ---------------------------------------------------------------- Anlegen

class CreateBody(BaseModel):
    template_id: int | None = None
    iso_file: str | None = Field(default=None, max_length=160)  # eigene ISO aus der Bibliothek des Nodes
    name: str = Field(min_length=1, max_length=64)
    hostname: str = Field(pattern=HOSTNAME)
    cores: int = Field(ge=1, le=128)
    memory_mb: int = Field(ge=256, le=1024 * 1024)
    disk_gb: int = Field(ge=1, le=16 * 1024)
    node: str | int = "auto"
    ipv4_pool: str | int = "auto"
    ipv6_pool: str | int = "none"
    password: str | None = Field(default=None, max_length=128)
    ssh_keys: str = ""
    owner_id: int | None = None
    storage_id: int | None = None  # gemeinsamer Speicher (None = lokal auf dem Node)
    app_id: str | None = Field(default=None, max_length=40)  # Anwendung mitinstallieren (apps.py)
    app_params: dict = {}


@router.post("")
def create_guest(body: CreateBody, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    template = None
    if body.iso_file:
        if not str(body.node).isdigit():
            raise HTTPException(400, "Bei eigener ISO bitte den Node wählen, auf dem die ISO liegt")
        node_for_iso = db.get(Node, int(body.node))
        if not node_for_iso:
            raise HTTPException(400, "Node nicht gefunden")
        try:
            available = {i["file"] for i in AgentClient.for_node(node_for_iso).request("GET", "/isos")}
        except AgentError as exc:
            raise HTTPException(502, str(exc))
        if body.iso_file not in available:
            raise HTTPException(400, f"ISO {body.iso_file} liegt nicht auf Node {node_for_iso.name}")
        gtype = "kvm"
    else:
        template = db.get(Template, body.template_id) if body.template_id else None
        if not template or (not template.enabled and not user.is_admin):
            raise HTTPException(400, "Vorlage nicht gefunden")
        if body.disk_gb < template.min_disk_gb:
            raise HTTPException(400, f"Diese Vorlage braucht mindestens {template.min_disk_gb} GB Speicher")
        gtype = template.type
    if gtype == "lxc" and body.memory_mb < 256:
        raise HTTPException(400, "Container brauchen mindestens 256 MB RAM")
    if body.password and len(body.password) < 8:
        raise HTTPException(400, "Das Passwort muss mindestens 8 Zeichen lang sein")
    owner = user
    if body.owner_id and body.owner_id != user.id:
        if not user.is_admin:
            raise HTTPException(403, "Nur Administratoren können Server für andere anlegen")
        owner = db.get(User, body.owner_id)
        if not owner:
            raise HTTPException(400, "Besitzer nicht gefunden")
    password = body.password or generate_password()
    keys = _keys(body.ssh_keys) or _keys(owner.ssh_keys)
    storage = cluster.get_visible(db, user, body.storage_id)
    app = apps.get(body.app_id)
    app_params = apps.validate(app, body.app_params, gtype, template, body.cores, body.memory_mb, body.disk_gb,
                               body.hostname) if app else {}

    with ipam.alloc_lock:
        node, v4, v6 = ops.resolve_placement(db, owner if not user.is_admin else user, gtype,
                                             body.node, body.ipv4_pool, body.ipv6_pool,
                                             storage.id if storage else None)
        pools = [p for p in (v4, v6) if p]
        ops.check_quota(db, owner, guests=1, cores=body.cores, memory_mb=body.memory_mb,
                        disk_gb=body.disk_gb, ips=len(pools))
        guest = Guest(vmid=ops.next_vmid(db), name=body.name.strip(), hostname=body.hostname,
                      type=gtype, node_id=node.id, owner_id=owner.id,
                      template_id=template.id if template else None, iso_file=body.iso_file,
                      cores=body.cores, memory_mb=body.memory_mb, disk_gb=body.disk_gb, mac=ops.new_mac(db),
                      storage_id=storage.id if storage else None, status="creating", power="unknown",
                      app_id=app["id"] if app else None, app_params=json.dumps(app_params))
        db.add(guest)
        db.flush()
        for pool in pools:
            ip = ipam.allocate(db, pool, guest.id)
            if not ip:
                db.rollback()
                raise HTTPException(400, f"Pool {pool.name} hat keine freien Adressen mehr")
            fixed_mac = ipam.pool_macs(pool).get(ip.address)
            if fixed_mac:  # IP ist beim Hoster an eine bestimmte MAC gebunden
                if db.scalar(select(Guest.id).where(Guest.mac == fixed_mac, Guest.id != guest.id)):
                    db.rollback()
                    raise HTTPException(400, f"Die MAC {fixed_mac} für {ip.address} nutzt bereits ein anderer Server")
                guest.mac = fixed_mac
        task = create_task(db, user.id, "create", f"{guest.name} (#{guest.vmid})", node.id, guest.id)
        audit(db, user, "guest-create", f"#{guest.vmid} {guest.name} {template.name if template else body.iso_file} auf {node.name}",
              client_ip(request))
        db.commit()
    submit(run_task(task.id, ops.op_create(task.id, guest.id, password, keys)))
    return {"guest_id": guest.id, "task_id": task.id, "password": password, "vmid": guest.vmid}


# ---------------------------------------------------------------- Ändern

class PatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=10000)
    owner_id: int | None = None
    ha: bool | None = None
    onboot: bool | None = None
    protected: bool | None = None
    tags: list[str] | None = None


TAG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,23}$")


@router.patch("/{guest_id}")
def patch_guest(guest_id: int, body: PatchBody, request: Request, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if body.name is not None:
        g.name = body.name.strip()
    if body.notes is not None:
        g.notes = body.notes
    if body.owner_id is not None and body.owner_id != g.owner_id:
        if not user.is_admin:
            raise HTTPException(403, "Nur Administratoren können den Besitzer ändern")
        if not db.get(User, body.owner_id):
            raise HTTPException(400, "Benutzer nicht gefunden")
        audit(db, user, "guest-owner", f"#{g.vmid} -> Benutzer {body.owner_id}", client_ip(request))
        g.owner_id = body.owner_id
    if body.tags is not None:
        tags = []
        for raw in body.tags:
            t = raw.strip().lower().replace(" ", "-")
            if t and t not in tags:
                if not TAG.match(t):
                    raise HTTPException(400, f"Ungültiger Tag „{raw}“ (a–z, 0–9, - _ ., max. 24 Zeichen)")
                tags.append(t)
        if len(tags) > 10:
            raise HTTPException(400, "Höchstens 10 Tags")
        g.tags = ",".join(tags)
    if body.protected is not None and body.protected != g.protected:
        g.protected = body.protected
        audit(db, user, "guest-protect", f"#{g.vmid} Löschschutz {'an' if body.protected else 'aus'}", client_ip(request))
    if body.onboot is not None and body.onboot != g.onboot:
        if g.ha:
            raise HTTPException(400, "Bei HA-Servern entscheidet das Panel über den Start – erst HA ausschalten")
        _ensure_ready(g)
        _call(lambda: _agent(g).request("PUT", f"/guests/{g.agent_name}/autostart", {"enabled": body.onboot}, timeout=30))
        g.onboot = body.onboot
    if body.ha is not None and body.ha != g.ha:
        if not user.is_admin:
            raise HTTPException(403, "Nur Administratoren können HA ein- oder ausschalten")
        if body.ha and not g.storage_id:
            raise HTTPException(400, "HA geht nur für Server auf gemeinsamem Speicher")
        g.ha = body.ha
        audit(db, user, "guest-ha", f"#{g.vmid} {'an' if body.ha else 'aus'}", client_ip(request))
        db.commit()
        try:
            cluster.sync_node_now(g.node_id)
        except AgentError as exc:
            raise HTTPException(502, f"Gespeichert, aber Node nicht erreichbar: {exc}")
    db.commit()
    return guest_dict(g)


class ActionBody(BaseModel):
    action: str = Field(pattern="^(start|stop|shutdown|reboot|suspend|resume)$")


@router.post("/{guest_id}/action")
def guest_action(guest_id: int, body: ActionBody, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    result = _call(lambda: _agent(g).request("POST", f"/guests/{g.agent_name}/action",
                                             {"action": body.action}, timeout=150))
    g.power = result.get("state", g.power)
    audit(db, user, f"guest-{body.action}", f"#{g.vmid} {g.name}", client_ip(request))
    db.commit()
    return {"state": g.power}


class ResizeBody(BaseModel):
    cores: int = Field(ge=1, le=128)
    memory_mb: int = Field(ge=256, le=1024 * 1024)
    disk_gb: int = Field(ge=1, le=16 * 1024)


@router.post("/{guest_id}/resize")
def resize_guest(guest_id: int, body: ResizeBody, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    if body.disk_gb < g.disk_gb:
        raise HTTPException(400, "Speicher kann nur vergrößert werden")
    ops.check_quota(db, g.owner, cores=body.cores, memory_mb=body.memory_mb, disk_gb=body.disk_gb,
                    exclude_guest=g.id)
    payload = {"cores": body.cores if body.cores != g.cores else None,
               "memory_mb": body.memory_mb if body.memory_mb != g.memory_mb else None,
               "disk_gb": body.disk_gb if body.disk_gb != g.disk_gb else None}
    if not any(payload.values()):
        return {"task_id": None}
    task = create_task(db, user.id, "resize", f"{g.name} (#{g.vmid})", g.node_id, g.id)
    audit(db, user, "guest-resize", f"#{g.vmid} {body.cores} CPU / {body.memory_mb} MB / {body.disk_gb} GB",
          client_ip(request))
    db.commit()

    def after(_result, gid=g.id, b=body):
        with session_scope() as s:
            guest = s.get(Guest, gid)
            guest.cores, guest.memory_mb, guest.disk_gb = b.cores, b.memory_mb, b.disk_gb

    submit(run_task(task.id, ops.op_job(task.id, g.id, "POST", "/guests/{name}/resize", payload, after)))
    return {"task_id": task.id}


class PasswordBody(BaseModel):
    password: str | None = Field(default=None, max_length=128)


@router.post("/{guest_id}/password")
def reset_password(guest_id: int, body: PasswordBody, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    password = body.password or generate_password()
    if len(password) < 8:
        raise HTTPException(400, "Das Passwort muss mindestens 8 Zeichen lang sein")
    _call(lambda: _agent(g).request("POST", f"/guests/{g.agent_name}/password",
                                    {"user": "root", "password": password}, timeout=90))
    audit(db, user, "guest-password", f"#{g.vmid} {g.name}", client_ip(request))
    db.commit()
    return {"password": password}


class NetworkBody(BaseModel):
    ipv4_pool: str | int = "keep"  # keep | none | <Pool-ID>
    ipv4_address: str | None = None  # bestimmte Adresse aus dem Pool (optional)
    ipv6_pool: str | int = "keep"
    ipv6_address: str | None = None
    mac: str | None = None  # nur Admin


def _pick(db: Session, user: User, g: Guest, version: int, choice, address: str | None, node: Node | None = None):
    """Liefert (Pool, Adresse) für eine neue IP. Wirft HTTPException bei ungültiger Wahl."""
    pool = db.get(IPPool, int(choice)) if str(choice).isdigit() else None
    if not pool or (pool.admin_only and not user.is_admin):
        raise HTTPException(400, "IP-Pool nicht gefunden")
    if ipam.version_of(pool) != version:
        raise HTTPException(400, f"{pool.name} ist kein IPv{version}-Pool")
    node = node or g.node
    if not ops.pool_fits_node(pool, node):
        raise HTTPException(400, f"Pool {pool.name} ist auf Node {node.name} nicht verfügbar")
    if address:
        try:
            address = str(ipaddress.ip_address(address.strip()))
        except ValueError:
            raise HTTPException(400, f"Ungültige Adresse: {address}")
        if not ipam.contains(pool, address):
            raise HTTPException(400, f"{address} gehört nicht zum Pool {pool.name}")
        if pool.gateway and address == pool.gateway:
            raise HTTPException(400, f"{address} ist das Gateway des Pools")
        row = db.scalar(select(IPAddress).where(IPAddress.pool_id == pool.id, IPAddress.address == address))
        if row and row.guest_id != g.id:
            raise HTTPException(400, f"{address} ist bereits vergeben oder reserviert")
        return pool, address
    free = ipam.free_address(db, pool)
    if not free:
        raise HTTPException(400, f"Pool {pool.name} hat keine freien Adressen mehr")
    return pool, free


@router.put("/{guest_id}/network")
def change_network(guest_id: int, body: NetworkBody, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """IP-Adressen tauschen, Pool wechseln (auch geroutet <-> Bridge) und MAC ändern.
    Neue Adressen werden sofort reserviert, alte erst nach erfolgreichem Umbau freigegeben."""
    g = guest_for(db, user, guest_id)
    if body.mac and not user.is_admin:
        raise HTTPException(403, "Nur Administratoren können die MAC-Adresse ändern")
    mac = ipam.normalize_mac(body.mac) if body.mac else None
    _ensure_ready(g)
    with ipam.alloc_lock:
        old_rows = list(g.ips)
        keep, drop, wanted = [], [], []
        choices = ((4, body.ipv4_pool, body.ipv4_address), (6, body.ipv6_pool, body.ipv6_address))
        for version, choice, address in choices:
            current = [ip for ip in old_rows if ipam.version_of(ip.pool) == version]
            if choice == "keep":
                keep += current
                continue
            if choice not in ("none", "", None):
                pool, address = _pick(db, user, g, version, choice, address)
                same = [ip for ip in current if ip.pool_id == pool.id and ip.address == address]
                if same:
                    keep += same
                    current = [ip for ip in current if ip not in same]
                else:
                    wanted.append((pool, address))
            drop += current
        pools = [ip.pool for ip in keep] + [p for p, _ in wanted]
        bridges = {p.bridge for p in pools if not ipam.is_routed(p)}
        if len({ipam.is_routed(p) for p in pools}) > 1 or len(bridges) > 1:
            raise HTTPException(400, "Alle IPs eines Servers müssen im selben Modus sein (geroutet bzw. dieselbe Bridge)")
        ops.check_quota(db, g.owner, ips=len(keep) + len(wanted), exclude_guest=g.id)
        if not mac:
            fixed = [ipam.pool_macs(p).get(a) for p, a in wanted]
            mac = next((m for m in fixed if m), g.mac)
        if db.scalar(select(Guest.id).where(Guest.mac == mac, Guest.id != g.id)):
            raise HTTPException(400, f"Die MAC {mac} nutzt bereits ein anderer Server")
        if not wanted and not drop and mac == g.mac:
            return {"task_id": None}
        new_rows = []
        for pool, address in wanted:
            existing = db.scalar(select(IPAddress).where(IPAddress.pool_id == pool.id, IPAddress.address == address))
            row = existing or IPAddress(pool_id=pool.id, address=address)
            row.guest_id, row.reserved, row.note = g.id, False, ""
            db.add(row)
            new_rows.append(row)
        for row in drop:
            row.guest_id, row.reserved, row.note = None, True, f"wird freigegeben (Umbau #{g.vmid})"
        db.flush()
        final = sorted(keep + new_rows, key=lambda r: ipam.version_of(r.pool))
        first = final[0].pool if final else None
        routed = bool(first and ipam.is_routed(first))
        bridge = ipam.ROUTED_BRIDGE if routed else first.bridge if first else ops.default_bridge(g.node)
        payload = {"mac": mac, "hostname": g.hostname, "routed": routed, "bridge": bridge,
                   "ips": [ipam.ip_spec(r, g.node) for r in final], "dns": ipam.pool_dns(first)}
        summary = ", ".join(r.address for r in final) or "DHCP"
        task = create_task(db, user.id, "network", f"{g.name} (#{g.vmid}): {summary}", g.node_id, g.id)
        audit(db, user, "guest-network", f"#{g.vmid} -> {summary}, MAC {mac}", client_ip(request))
        db.commit()
    new_ids, drop_ids = [r.id for r in new_rows], [r.id for r in drop]

    def after(_result, gid=g.id):
        with session_scope() as s:
            s.get(Guest, gid).mac = mac
            for rid in drop_ids:
                row = s.get(IPAddress, rid)
                if row:
                    s.delete(row)
        from .extras import push_firewall
        push_firewall(gid)

    def rollback(_msg, gid=g.id):
        with session_scope() as s:
            for rid in new_ids:
                row = s.get(IPAddress, rid)
                if row:
                    s.delete(row)
            for rid in drop_ids:
                row = s.get(IPAddress, rid)
                if row:
                    row.guest_id, row.reserved, row.note = gid, False, ""

    submit(run_task(task.id, ops.op_job(task.id, g.id, "POST", "/guests/{name}/network", payload, after),
                    on_error=rollback))
    return {"task_id": task.id}


class CdromBody(BaseModel):
    iso_file: str | None = Field(default=None, max_length=160)
    boot_cdrom: bool = False


@router.get("/{guest_id}/cdrom")
def get_cdrom(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if g.type != "kvm":
        raise HTTPException(400, "Nur VMs haben ein CD-Laufwerk")
    return _call(lambda: _agent(g).request("GET", f"/guests/{g.agent_name}/cdrom"))


@router.post("/{guest_id}/cdrom")
def set_cdrom(guest_id: int, body: CdromBody, request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if g.type != "kvm":
        raise HTTPException(400, "Nur VMs haben ein CD-Laufwerk")
    _ensure_ready(g)
    result = _call(lambda: _agent(g).request("POST", f"/guests/{g.agent_name}/cdrom", body.model_dump(), timeout=60))
    audit(db, user, "guest-cdrom", f"#{g.vmid} {body.iso_file or 'ausgeworfen'}, Boot von CD: {body.boot_cdrom}",
          client_ip(request))
    db.commit()
    return result


class MigrateBody(BaseModel):
    node_id: int
    ipv4_pool: str | int = "keep"  # keep (nur wenn der Pool auf dem Ziel verfügbar ist) | none | <Pool-ID>
    ipv4_address: str | None = None
    ipv6_pool: str | int = "keep"
    ipv6_address: str | None = None


@router.get("/{guest_id}/migrate/check")
def migrate_check(guest_id: int, node_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Welche IPs auf dem Ziel-Node weiterverwendet werden können."""
    if not user.is_admin:
        raise HTTPException(403, "Nur Administratoren können Server umziehen")
    g = guest_for(db, user, guest_id)
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node nicht gefunden")
    return {"ips": [{"address": ip.address, "pool": ip.pool.name, "version": ipam.version_of(ip.pool),
                     "usable": ops.pool_fits_node(ip.pool, node)} for ip in g.ips],
            "shared": bool(g.storage_id and cluster.mounted_on(node.id, g.storage_id)),
            "storage": g.storage.name if g.storage else None}


@router.post("/{guest_id}/migrate")
def migrate_guest(guest_id: int, body: MigrateBody, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    if not user.is_admin:
        raise HTTPException(403, "Nur Administratoren können Server umziehen")
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    target = db.get(Node, body.node_id)
    if not target or target.id == g.node_id:
        raise HTTPException(400, "Bitte einen anderen Node wählen")
    if target.status != "online":
        raise HTTPException(400, f"Node {target.name} ist nicht online")
    if not ops.node_supports(target, g.type):
        raise HTTPException(400, f"Node {target.name} unterstützt {'VMs' if g.type == 'kvm' else 'Container'} nicht")
    if g.power == "missing":
        raise HTTPException(400, "Server auf dem aktuellen Node nicht gefunden")
    if g.storage_id and not cluster.mounted_on(target.id, g.storage_id):
        raise HTTPException(400, f"Speicher {g.storage.name} ist auf {target.name} nicht eingehängt")
    with ipam.alloc_lock:
        keep, drop, wanted = [], [], []
        choices = ((4, body.ipv4_pool, body.ipv4_address), (6, body.ipv6_pool, body.ipv6_address))
        for version, choice, address in choices:
            current = [ip for ip in g.ips if ipam.version_of(ip.pool) == version]
            if choice == "keep":
                for ip in current:
                    if not ops.pool_fits_node(ip.pool, target):
                        raise HTTPException(400, f"{ip.address} (Pool {ip.pool.name}) ist auf {target.name} nicht nutzbar "
                                                 "– bitte eine neue IP wählen")
                keep += current
                continue
            if choice not in ("none", "", None):
                wanted.append(_pick(db, user, g, version, choice, address, node=target))
            drop += current
        pools = [ip.pool for ip in keep] + [p for p, _ in wanted]
        if len({ipam.is_routed(p) for p in pools}) > 1 or len({p.bridge for p in pools if not ipam.is_routed(p)}) > 1:
            raise HTTPException(400, "Alle IPs eines Servers müssen im selben Modus sein (geroutet bzw. dieselbe Bridge)")
        ops.check_quota(db, g.owner, ips=len(keep) + len(wanted), exclude_guest=g.id)
        fixed = [ipam.pool_macs(p).get(a) for p, a in wanted]
        mac = next((m for m in fixed if m), g.mac)
        new_rows = []
        for pool, address in wanted:
            row = db.scalar(select(IPAddress).where(IPAddress.pool_id == pool.id, IPAddress.address == address)) \
                or IPAddress(pool_id=pool.id, address=address)
            row.guest_id, row.reserved, row.note = g.id, False, ""
            db.add(row)
            new_rows.append(row)
        for row in drop:
            row.guest_id, row.reserved, row.note = None, True, f"wird freigegeben (Umzug #{g.vmid})"
        db.flush()
        final = sorted(keep + new_rows, key=lambda r: ipam.version_of(r.pool))
        first = final[0].pool if final else None
        routed = bool(first and ipam.is_routed(first))
        network = {"mac": mac, "hostname": g.hostname, "routed": routed,
                   "bridge": ipam.ROUTED_BRIDGE if routed else first.bridge if first else ops.default_bridge(target),
                   "ips": [ipam.ip_spec(r, target) for r in final], "dns": ipam.pool_dns(first)}
        task = create_task(db, user.id, "migrate", f"{g.name} (#{g.vmid}): {g.node.name} → {target.name}", target.id, g.id)
        audit(db, user, "guest-migrate", f"#{g.vmid} {g.node.name} -> {target.name}", client_ip(request))
        db.commit()
    submit(run_task(task.id, ops.op_migrate(task.id, g.id, target.id, network, [r.id for r in new_rows],
                                            [r.id for r in drop], shared=bool(g.storage_id))))
    return {"task_id": task.id}


@router.get("/{guest_id}/app")
def guest_app(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Fortschritt und Zugangsdaten der mitinstallierten Anwendung (aus dem Server gelesen)."""
    g = guest_for(db, user, guest_id)
    if not g.app_id:
        raise HTTPException(404, "Keine Anwendung installiert")
    if g.status == "creating":
        return {"state": "creating", "log": [], "info": ""}
    return _call(lambda: _agent(g).request("GET", f"/guests/{g.agent_name}/app", timeout=60))


class CloneBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    hostname: str = Field(pattern=HOSTNAME)
    ipv4_pool: str | int = "auto"
    ipv6_pool: str | int = "auto"  # auto = wie die Quelle (falls sie IPv6 hat)
    password: str | None = Field(default=None, max_length=128)


@router.post("/{guest_id}/clone")
def clone_guest(guest_id: int, body: CloneBody, request: Request, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """Vollständige Kopie auf demselben Node – zählt wie ein neuer Server gegen das Kontingent."""
    src = guest_for(db, user, guest_id)
    _ensure_ready(src)
    if body.password and len(body.password) < 8:
        raise HTTPException(400, "Das Passwort muss mindestens 8 Zeichen lang sein")
    owner = src.owner
    has6 = any(ipam.version_of(ip.pool) == 6 for ip in src.ips)
    v6_choice = body.ipv6_pool if body.ipv6_pool != "auto" or has6 else "none"
    if not src.ips and body.ipv4_pool == "auto":
        v4_choice = "none"
    else:
        v4_choice = body.ipv4_pool
    password = body.password or generate_password()
    keys = _keys(owner.ssh_keys)
    with ipam.alloc_lock:
        node, v4, v6 = ops.resolve_placement(db, user if user.is_admin else owner, src.type, src.node_id,
                                             v4_choice, v6_choice, src.storage_id)
        pools = [p for p in (v4, v6) if p]
        ops.check_quota(db, owner, guests=1, cores=src.cores, memory_mb=src.memory_mb, disk_gb=src.disk_gb,
                        ips=len(pools))
        g = Guest(vmid=ops.next_vmid(db), name=body.name.strip(), hostname=body.hostname, type=src.type,
                  node_id=node.id, owner_id=owner.id, template_id=src.template_id, iso_file=src.iso_file,
                  cores=src.cores, memory_mb=src.memory_mb, disk_gb=src.disk_gb, mac=ops.new_mac(db),
                  storage_id=src.storage_id, app_id=src.app_id, app_params=src.app_params, tags=src.tags,
                  notes=src.notes, status="creating", power="unknown")
        db.add(g)
        db.flush()
        for pool in pools:
            ip = ipam.allocate(db, pool, g.id)
            if not ip:
                db.rollback()
                raise HTTPException(400, f"Pool {pool.name} hat keine freien Adressen mehr")
            fixed_mac = ipam.pool_macs(pool).get(ip.address)
            if fixed_mac:
                if db.scalar(select(Guest.id).where(Guest.mac == fixed_mac, Guest.id != g.id)):
                    db.rollback()
                    raise HTTPException(400, f"Die MAC {fixed_mac} für {ip.address} nutzt bereits ein anderer Server")
                g.mac = fixed_mac
        # Firewall-Regeln der Quelle übernehmen (Spoofing-Schutz gilt automatisch für die neuen IPs)
        from ..models import FirewallConfig
        fw = db.get(FirewallConfig, src.id)
        if fw:
            db.add(FirewallConfig(guest_id=g.id, enabled=fw.enabled, policy_in=fw.policy_in, policy_out=fw.policy_out,
                                  antispoof=fw.antispoof, rules_json=fw.rules_json))
        task = create_task(db, user.id, "clone", f"{src.name} (#{src.vmid}) → {g.name} (#{g.vmid})", node.id, g.id)
        audit(db, user, "guest-clone", f"#{src.vmid} -> #{g.vmid} {g.name}", client_ip(request))
        db.commit()
    submit(run_task(task.id, ops.op_clone(task.id, src.id, g.id, password, keys)))
    return {"guest_id": g.id, "task_id": task.id, "password": password, "vmid": g.vmid}


class ReinstallBody(BaseModel):
    template_id: int | None = None
    password: str | None = Field(default=None, max_length=128)
    ssh_keys: str = ""
    app: str = Field(default="keep", max_length=40)  # keep | none | <Anwendungs-ID>
    app_params: dict = {}


@router.post("/{guest_id}/reinstall")
def reinstall_guest(guest_id: int, body: ReinstallBody, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    if g.protected:
        raise HTTPException(400, "Löschschutz ist aktiv – Neuinstallation nicht möglich")
    if body.template_id:
        tpl = db.get(Template, body.template_id)
        if not tpl or (not tpl.enabled and not user.is_admin):
            raise HTTPException(400, "Vorlage nicht gefunden")
        if tpl.type != g.type:
            raise HTTPException(400, "Die Vorlage muss vom selben Typ sein (VM bzw. Container)")
        if g.disk_gb < tpl.min_disk_gb:
            raise HTTPException(400, f"Diese Vorlage braucht mindestens {tpl.min_disk_gb} GB")
        g.template_id, g.iso_file = tpl.id, None
    if not g.template_id and not g.iso_file:
        raise HTTPException(400, "Bitte eine Vorlage wählen")
    if body.app == "none":
        g.app_id, g.app_params = None, ""
    elif body.app != "keep" or g.app_id:
        app = apps.get(g.app_id if body.app == "keep" else body.app)
        params = json.loads(g.app_params or "{}") if body.app == "keep" else body.app_params
        new_tpl = db.get(Template, g.template_id) if g.template_id and not g.iso_file else None  # ggf. gerade geändert
        clean = apps.validate(app, params, g.type, new_tpl, g.cores, g.memory_mb,
                              g.disk_gb, g.hostname)
        g.app_id, g.app_params = app["id"], json.dumps(clean)
    password = body.password or generate_password()
    keys = _keys(body.ssh_keys) or _keys(g.owner.ssh_keys)
    task = create_task(db, user.id, "reinstall", f"{g.name} (#{g.vmid})", g.node_id, g.id)
    audit(db, user, "guest-reinstall", f"#{g.vmid} {g.name}", client_ip(request))
    db.commit()
    submit(run_task(task.id, ops.op_reinstall(task.id, g.id, password, keys)))
    return {"task_id": task.id, "password": password}


@router.delete("/{guest_id}")
def delete_guest(guest_id: int, request: Request, force: bool = False, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    if g.protected:
        raise HTTPException(400, "Löschschutz ist aktiv – erst in den Einstellungen des Servers ausschalten")
    if force and not user.is_admin:
        raise HTTPException(403, "Erzwungenes Löschen nur für Administratoren")
    if g.status in ("creating", "deleting") and not force:
        raise HTTPException(409, "Der Server ist gerade beschäftigt")
    task = create_task(db, user.id, "delete", f"{g.name} (#{g.vmid})", g.node_id, g.id)
    audit(db, user, "guest-delete", f"#{g.vmid} {g.name}", client_ip(request))
    db.commit()
    submit(run_task(task.id, ops.op_delete(task.id, g.id, force)))
    return {"task_id": task.id}


# ---------------------------------------------------------------- Snapshots

class SnapshotBody(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    description: str = Field(default="", max_length=200)


@router.get("/{guest_id}/snapshots")
def list_snapshots(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    return _call(lambda: _agent(g).request("GET", f"/guests/{g.agent_name}/snapshots"))


@router.post("/{guest_id}/snapshots")
def create_snapshot(guest_id: int, body: SnapshotBody, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    if not user.is_admin:
        existing = _call(lambda: _agent(g).request("GET", f"/guests/{g.agent_name}/snapshots"))
        if len(existing) >= USER_SNAPSHOT_LIMIT:
            raise HTTPException(400, f"Maximal {USER_SNAPSHOT_LIMIT} Snapshots pro Server")
    task = create_task(db, user.id, "snapshot", f"{g.name}: {body.name}", g.node_id, g.id)
    audit(db, user, "snapshot-create", f"#{g.vmid} {body.name}", client_ip(request))
    db.commit()
    submit(run_task(task.id, ops.op_job(task.id, g.id, "POST", "/guests/{name}/snapshots", body.model_dump())))
    return {"task_id": task.id}


def _snap_op(guest_id, snap, request, user, db, action, method, suffix):
    if not re.match(r"^[A-Za-z0-9_-]{1,40}$", snap):
        raise HTTPException(400, "Ungültiger Snapshot-Name")
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    task = create_task(db, user.id, action, f"{g.name}: {snap}", g.node_id, g.id)
    audit(db, user, action, f"#{g.vmid} {snap}", client_ip(request))
    db.commit()
    submit(run_task(task.id, ops.op_job(task.id, g.id, method, f"/guests/{{name}}/snapshots/{snap}{suffix}")))
    return {"task_id": task.id}


@router.delete("/{guest_id}/snapshots/{snap}")
def delete_snapshot(guest_id: int, snap: str, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    return _snap_op(guest_id, snap, request, user, db, "snapshot-delete", "DELETE", "")


@router.post("/{guest_id}/snapshots/{snap}/rollback")
def rollback_snapshot(guest_id: int, snap: str, request: Request, user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    return _snap_op(guest_id, snap, request, user, db, "snapshot-rollback", "POST", "/rollback")


# ---------------------------------------------------------------- Backups

def _backup_name_ok(g: Guest, filename: str) -> None:
    pattern = rf"^{g.agent_name}-(kvm|lxc)-\d{{8}}-\d{{6}}(-auto)?\.tar(\.gz)?$"
    if not re.match(pattern, filename):
        raise HTTPException(404, "Backup nicht gefunden")


def _since(g: Guest) -> float:
    return g.created_at.replace(tzinfo=timezone.utc).timestamp() - 60


def _own_backups(g: Guest) -> list[dict]:
    """Nur Backups, die nach dem Anlegen dieses Servers entstanden sind (Schutz vor Altlasten mit gleicher VMID)."""
    backups = _call(lambda: _agent(g).request("GET", f"/backups?name={g.agent_name}"))
    return [b for b in backups if b["created"] >= _since(g)]


def _remote_backups(g: Guest, t) -> list[dict]:
    files = _agent(g).request("POST", "/remote/list", {"target": backup_targets.payload(t), "sub": backup_targets.subdir(g)},
                              timeout=120)
    return [f for f in files if f["created"] >= _since(g)]


@router.get("/{guest_id}/backups")
def list_backups(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Lokale und externe Backups eines Servers. `targets` enthält je Speicher ggf. eine Fehlermeldung."""
    g = guest_for(db, user, guest_id)
    items = [{**b, "location": None, "location_name": f"Lokal ({g.node.name})"} for b in _own_backups(g)]
    targets = []
    for t in backup_targets.visible(db, user):
        try:
            for f in _remote_backups(g, t):
                items.append({**f, "location": t.id, "location_name": t.name})
            targets.append({"id": t.id, "name": t.name, "type": t.type})
        except AgentError as exc:
            targets.append({"id": t.id, "name": t.name, "type": t.type, "error": str(exc)})
    items.sort(key=lambda b: b["created"], reverse=True)
    return {"items": items, "targets": targets}


class BackupBody(BaseModel):
    target_id: int | None = None  # None = lokal auf dem Node
    keep_local: bool = False       # bei externem Ziel zusätzlich lokal behalten


@router.post("/{guest_id}/backups")
def create_backup(guest_id: int, request: Request, body: BackupBody | None = None,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    body = body or BackupBody()
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    target = backup_targets.get_visible(db, user, body.target_id)
    if not user.is_admin:
        existing = list_backups(guest_id, user, db)["items"]
        if len([b for b in existing if not b.get("auto")]) >= USER_BACKUP_LIMIT:
            raise HTTPException(400, f"Maximal {USER_BACKUP_LIMIT} manuelle Backups pro Server – bitte alte löschen")
    where = target.name if target else "lokal"
    task = create_task(db, user.id, "backup", f"{g.name} (#{g.vmid}) → {where}", g.node_id, g.id)
    audit(db, user, "backup-create", f"#{g.vmid} → {where}", client_ip(request))
    db.commit()
    tp = backup_targets.payload(target) if target else None
    submit(run_task(task.id, ops.op_backup(task.id, g.id, tp, backup_targets.subdir(g), body.keep_local)))
    return {"task_id": task.id}


@router.delete("/{guest_id}/backups/{filename}")
def delete_backup(guest_id: int, filename: str, request: Request, target: int | None = None,
                  user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _backup_name_ok(g, filename)
    t = backup_targets.get_visible(db, user, target)
    if t:
        if filename not in {f["file"] for f in _call(lambda: _remote_backups(g, t))}:
            raise HTTPException(404, "Backup nicht gefunden")
        _call(lambda: _agent(g).request("POST", "/remote/delete", {"target": backup_targets.payload(t),
                                                                     "sub": backup_targets.subdir(g), "file": filename}))
    else:
        if filename not in {b["file"] for b in _own_backups(g)}:
            raise HTTPException(404, "Backup nicht gefunden")
        _call(lambda: _agent(g).request("DELETE", f"/backups/{filename}"))
    audit(db, user, "backup-delete", f"#{g.vmid} {filename} ({t.name if t else 'lokal'})", client_ip(request))
    db.commit()
    return {"ok": True}


@router.post("/{guest_id}/backups/{filename}/restore")
def restore_backup(guest_id: int, filename: str, request: Request, target: int | None = None,
                   user: User = Depends(current_user), db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    _backup_name_ok(g, filename)
    t = backup_targets.get_visible(db, user, target)
    local = {b["file"] for b in _own_backups(g)}
    if t:
        if filename not in {f["file"] for f in _call(lambda: _remote_backups(g, t))}:
            raise HTTPException(404, "Backup nicht gefunden")
    elif filename not in local:
        raise HTTPException(404, "Backup nicht gefunden")
    task = create_task(db, user.id, "restore", f"{g.name}: {filename}", g.node_id, g.id)
    audit(db, user, "backup-restore", f"#{g.vmid} {filename} ({t.name if t else 'lokal'})", client_ip(request))
    db.commit()
    tp = backup_targets.payload(t) if t else None
    submit(run_task(task.id, ops.op_restore(task.id, g.id, filename, tp, backup_targets.subdir(g),
                                            already_local=filename in local)))
    return {"task_id": task.id}


# ---------------------------------------------------------------- Konsole (WebSocket-Proxy)

def same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    return bool(origin) and urlparse(origin).netloc == ws.headers.get("host")


async def proxy_websocket(ws: WebSocket, client: AgentClient, path: str) -> None:
    subprotocol = "binary" if "binary" in ws.scope.get("subprotocols", []) else None
    try:
        upstream = await ws_connect(client.ws_base + path, ssl=client.ctx, max_size=None,
                                    additional_headers=client.headers, open_timeout=15)
    except Exception as exc:  # noqa: BLE001
        await ws.accept(subprotocol=subprotocol)
        await ws.send_text(f"\r\nKonsole nicht erreichbar: {exc}\r\n")
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=subprotocol)

    async def browser_to_agent():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def agent_to_browser():
        async for data in upstream:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_text(data)

    tasks = [asyncio.create_task(browser_to_agent()), asyncio.create_task(agent_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        try:
            await ws.close()
        except RuntimeError:
            pass


def ws_user(ws: WebSocket, db: Session) -> User | None:
    uid = ws.session.get("uid") if "session" in ws.scope else None
    user = db.get(User, uid) if uid else None
    return user if user and user.active else None


@router.websocket("/{guest_id}/console")
async def guest_console(ws: WebSocket, guest_id: int):
    if not same_origin(ws):
        await ws.close(code=4403)
        return
    with SessionLocal() as db:
        user = ws_user(ws, db)
        g = db.get(Guest, guest_id)
        if not user or not g or (not user.is_admin and g.owner_id != user.id):
            await ws.close(code=4403)
            return
        client, name = AgentClient.for_node(g.node), g.agent_name
    await proxy_websocket(ws, client, f"/guests/{name}/console")
