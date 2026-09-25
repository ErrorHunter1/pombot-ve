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

from .. import ipam, ops
from ..agent_client import AgentClient, AgentError
from ..db import SessionLocal, get_db, session_scope
from ..models import Guest, IPAddress, IPPool, Template, User
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
        "template_id": g.template_id, "template": g.template.name if g.template else None,
        "template_source": g.template.source if g.template else None,
        "cores": g.cores, "memory_mb": g.memory_mb, "disk_gb": g.disk_gb, "mac": g.mac,
        "status": g.status, "power": g.power, "error": g.error, "notes": g.notes,
        "created_at": g.created_at.isoformat() + "Z",
        "ips": [_ip_view(ip, g) for ip in g.ips],
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
    template_id: int
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


@router.post("")
def create_guest(body: CreateBody, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    template = db.get(Template, body.template_id)
    if not template or (not template.enabled and not user.is_admin):
        raise HTTPException(400, "Vorlage nicht gefunden")
    if body.disk_gb < template.min_disk_gb:
        raise HTTPException(400, f"Diese Vorlage braucht mindestens {template.min_disk_gb} GB Speicher")
    if template.type == "lxc" and body.memory_mb < 256:
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

    with ipam.alloc_lock:
        node, v4, v6 = ops.resolve_placement(db, owner if not user.is_admin else user, template.type,
                                             body.node, body.ipv4_pool, body.ipv6_pool)
        pools = [p for p in (v4, v6) if p]
        ops.check_quota(db, owner, guests=1, cores=body.cores, memory_mb=body.memory_mb,
                        disk_gb=body.disk_gb, ips=len(pools))
        guest = Guest(vmid=ops.next_vmid(db), name=body.name.strip(), hostname=body.hostname,
                      type=template.type, node_id=node.id, owner_id=owner.id, template_id=template.id,
                      cores=body.cores, memory_mb=body.memory_mb, disk_gb=body.disk_gb, mac=ops.new_mac(db),
                      status="creating", power="unknown")
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
        audit(db, user, "guest-create", f"#{guest.vmid} {guest.name} {template.name} auf {node.name}",
              client_ip(request))
        db.commit()
    submit(run_task(task.id, ops.op_create(task.id, guest.id, password, keys)))
    return {"guest_id": guest.id, "task_id": task.id, "password": password, "vmid": guest.vmid}


# ---------------------------------------------------------------- Ändern

class PatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    notes: str | None = Field(default=None, max_length=10000)
    owner_id: int | None = None


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


def _pick(db: Session, user: User, g: Guest, version: int, choice, address: str | None):
    """Liefert (Pool, Adresse) für eine neue IP. Wirft HTTPException bei ungültiger Wahl."""
    pool = db.get(IPPool, int(choice)) if str(choice).isdigit() else None
    if not pool or (pool.admin_only and not user.is_admin):
        raise HTTPException(400, "IP-Pool nicht gefunden")
    if ipam.version_of(pool) != version:
        raise HTTPException(400, f"{pool.name} ist kein IPv{version}-Pool")
    if not ops.pool_fits_node(pool, g.node):
        raise HTTPException(400, f"Pool {pool.name} ist auf Node {g.node.name} nicht verfügbar")
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
        if any(ipam.is_routed(p) and ipam.version_of(p) == 6 for p in pools):
            raise HTTPException(400, "Geroutete Pools unterstützen nur IPv4")
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


class ReinstallBody(BaseModel):
    template_id: int | None = None
    password: str | None = Field(default=None, max_length=128)
    ssh_keys: str = ""


@router.post("/{guest_id}/reinstall")
def reinstall_guest(guest_id: int, body: ReinstallBody, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    if body.template_id:
        tpl = db.get(Template, body.template_id)
        if not tpl or (not tpl.enabled and not user.is_admin):
            raise HTTPException(400, "Vorlage nicht gefunden")
        if tpl.type != g.type:
            raise HTTPException(400, "Die Vorlage muss vom selben Typ sein (VM bzw. Container)")
        if g.disk_gb < tpl.min_disk_gb:
            raise HTTPException(400, f"Diese Vorlage braucht mindestens {tpl.min_disk_gb} GB")
        g.template_id = tpl.id
    if not g.template_id:
        raise HTTPException(400, "Bitte eine Vorlage wählen")
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
    if not re.match(pattern, filename) or filename not in {b["file"] for b in _own_backups(g)}:
        raise HTTPException(404, "Backup nicht gefunden")


def _own_backups(g: Guest) -> list[dict]:
    """Nur Backups, die nach dem Anlegen dieses Servers entstanden sind (Schutz vor Altlasten mit gleicher VMID)."""
    since = g.created_at.replace(tzinfo=timezone.utc).timestamp() - 60
    backups = _call(lambda: _agent(g).request("GET", f"/backups?name={g.agent_name}"))
    return [b for b in backups if b["created"] >= since]


@router.get("/{guest_id}/backups")
def list_backups(guest_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _own_backups(guest_for(db, user, guest_id))


@router.post("/{guest_id}/backups")
def create_backup(guest_id: int, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    if not user.is_admin:
        if len([b for b in _own_backups(g) if not b.get("auto")]) >= USER_BACKUP_LIMIT:
            raise HTTPException(400, f"Maximal {USER_BACKUP_LIMIT} Backups pro Server – bitte alte löschen")
    task = create_task(db, user.id, "backup", f"{g.name} (#{g.vmid})", g.node_id, g.id)
    audit(db, user, "backup-create", f"#{g.vmid}", client_ip(request))
    db.commit()
    submit(run_task(task.id, ops.op_job(task.id, g.id, "POST", "/guests/{name}/backups")))
    return {"task_id": task.id}


@router.delete("/{guest_id}/backups/{filename}")
def delete_backup(guest_id: int, filename: str, request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _backup_name_ok(g, filename)
    _call(lambda: _agent(g).request("DELETE", f"/backups/{filename}"))
    audit(db, user, "backup-delete", f"#{g.vmid} {filename}", client_ip(request))
    db.commit()
    return {"ok": True}


@router.post("/{guest_id}/backups/{filename}/restore")
def restore_backup(guest_id: int, filename: str, request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    g = guest_for(db, user, guest_id)
    _ensure_ready(g)
    _backup_name_ok(g, filename)
    task = create_task(db, user.id, "restore", f"{g.name}: {filename}", g.node_id, g.id)
    audit(db, user, "backup-restore", f"#{g.vmid} {filename}", client_ip(request))
    db.commit()
    submit(run_task(task.id, ops.op_job(task.id, g.id, "POST", f"/backups/{filename}/restore")))
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
