"""IP-Pools und einzelne Adressen verwalten."""
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ipam
from ..db import get_db
from ..models import IPAddress, IPPool, Node, User
from ..perms import require
from ..security import audit, client_ip, current_user

router = APIRouter(prefix="/api/pools", tags=["pools"])


def pool_dict(db: Session, p: IPPool) -> dict:
    return {
        "id": p.id, "name": p.name, "network": p.network, "gateway": p.gateway, "dns": p.dns,
        "mode": p.mode, "address_list": p.address_list, "list_count": len(ipam.pool_list(p)),
        "bridge": p.bridge, "range_start": p.range_start, "range_end": p.range_end,
        "node_id": p.node_id, "node": p.node.name if p.node else None, "admin_only": p.admin_only,
        "version": ipam.version_of(p), "size": ipam.pool_size(p), "used": ipam.pool_used(db, p),
    }


class PoolBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="bridged", pattern="^(bridged|routed)$")
    network: str = ""
    address_list: str = Field(default="", max_length=100_000)
    gateway: str | None = None
    dns: str = ""
    bridge: str = Field(default="vmbr0", pattern=r"^[A-Za-z0-9_.-]{1,15}$")
    range_start: str | None = None
    range_end: str | None = None
    node_id: int | None = None
    admin_only: bool = False


def _clean(body: PoolBody, db: Session) -> dict:
    data = body.model_dump()
    for key in ("gateway", "range_start", "range_end"):
        data[key] = (data[key] or "").strip() or None
    data["network"] = ipam.validate_pool(data["mode"], data["network"], data["gateway"], data["range_start"],
                                         data["range_end"], data["address_list"])
    data["address_list"] = ipam.format_entries(ipam.parse_entries(data["address_list"]))
    if data["mode"] == "routed":
        # Gateway ist automatisch die Haupt-IP des Nodes; Gäste hängen an der internen Bridge pbr0
        data["gateway"], data["bridge"] = None, ipam.ROUTED_BRIDGE
        if not data["node_id"]:
            nodes = db.scalars(select(Node.id)).all()
            if len(nodes) != 1:
                raise HTTPException(400, "Geroutete Zusatz-IPs gehören zu genau einem Server – bitte den Node wählen")
            data["node_id"] = nodes[0]
    for server in [s.strip() for s in data["dns"].replace(";", ",").split(",") if s.strip()]:
        try:
            ipaddress.ip_address(server)
        except ValueError:
            raise HTTPException(400, f"Ungültiger DNS-Server: {server}")
    if data["node_id"] and not db.get(Node, data["node_id"]):
        raise HTTPException(400, "Node nicht gefunden")
    return data


@router.get("")
def list_pools(user: User = Depends(current_user), db: Session = Depends(get_db)):
    pools = db.scalars(select(IPPool).order_by(IPPool.name))
    return [pool_dict(db, p) for p in pools if user.can("pools.manage") or user.can("quota.unlimited") or not p.admin_only]


@router.post("")
def create_pool(body: PoolBody, request: Request, user: User = Depends(require("pools.manage")),
                db: Session = Depends(get_db)):
    if db.scalar(select(IPPool).where(IPPool.name == body.name)):
        raise HTTPException(400, "Name bereits vergeben")
    pool = IPPool(**_clean(body, db))
    db.add(pool)
    audit(db, user, "pool-create", f"{pool.name} {pool.network}", client_ip(request))
    db.commit()
    return pool_dict(db, pool)


@router.put("/{pool_id}")
def update_pool(pool_id: int, body: PoolBody, request: Request, user: User = Depends(require("pools.manage")),
                db: Session = Depends(get_db)):
    pool = db.get(IPPool, pool_id)
    if not pool:
        raise HTTPException(404, "Pool nicht gefunden")
    data = _clean(body, db)
    if pool.addresses and (data["mode"] != pool.mode or data["node_id"] != pool.node_id):
        raise HTTPException(400, "Modus und Node können nicht geändert werden, solange Adressen vergeben sind")
    for key, value in data.items():
        setattr(pool, key, value)
    missing = [a.address for a in pool.addresses if a.guest_id and not ipam.contains(pool, a.address)]
    if missing:
        db.rollback()
        raise HTTPException(400, f"Diese vergebenen Adressen wären nicht mehr im Pool: {', '.join(missing)}")
    audit(db, user, "pool-update", pool.name, client_ip(request))
    db.commit()
    return pool_dict(db, pool)


@router.delete("/{pool_id}")
def delete_pool(pool_id: int, request: Request, user: User = Depends(require("pools.manage")),
                db: Session = Depends(get_db)):
    pool = db.get(IPPool, pool_id)
    if not pool:
        raise HTTPException(404, "Pool nicht gefunden")
    if any(a.guest_id for a in pool.addresses):
        raise HTTPException(400, "Es sind noch Adressen an Server vergeben")
    audit(db, user, "pool-delete", pool.name, client_ip(request))
    db.delete(pool)
    db.commit()
    return {"ok": True}


@router.get("/{pool_id}/addresses")
def pool_addresses(pool_id: int, user: User = Depends(require("pools.manage")), db: Session = Depends(get_db)):
    pool = db.get(IPPool, pool_id)
    if not pool:
        raise HTTPException(404, "Pool nicht gefunden")
    rows = sorted(pool.addresses, key=lambda a: ipaddress.ip_address(a.address))
    macs = ipam.pool_macs(pool)
    return {
        "pool": pool_dict(db, pool),
        "next_free": ipam.free_address(db, pool),
        "addresses": [{
            "id": a.id, "address": a.address, "reserved": a.reserved, "note": a.note,
            "mac": macs.get(a.address), "guest_id": a.guest_id, "guest": f"{a.guest.name} (#{a.guest.vmid})" if a.guest else None,
            "owner": a.guest.owner.username if a.guest else None,
        } for a in rows],
    }


@router.get("/{pool_id}/free")
def free_addresses(pool_id: int, limit: int = 256, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """Freie Adressen eines Pools (für die Auswahl einer bestimmten IP)."""
    pool = db.get(IPPool, pool_id)
    if not pool or (pool.admin_only and not (user.can("pools.manage") or user.can("quota.unlimited"))):
        raise HTTPException(404, "Pool nicht gefunden")
    used = set(db.scalars(select(IPAddress.address).where(IPAddress.pool_id == pool.id)).all())
    macs = ipam.pool_macs(pool)
    result = []
    for ip in ipam.candidates(pool):
        if ip not in used:
            result.append({"address": ip, "mac": macs.get(ip)})
            if len(result) >= min(limit, 1024):
                break
    return result


class ReserveBody(BaseModel):
    address: str
    note: str = Field(default="", max_length=255)


@router.post("/{pool_id}/reserve")
def reserve_address(pool_id: int, body: ReserveBody, request: Request, user: User = Depends(require("pools.manage")),
                    db: Session = Depends(get_db)):
    pool = db.get(IPPool, pool_id)
    if not pool:
        raise HTTPException(404, "Pool nicht gefunden")
    try:
        addr = ipaddress.ip_address(body.address.strip())
    except ValueError:
        raise HTTPException(400, "Ungültige IP-Adresse")
    if not ipam.contains(pool, str(addr)):
        raise HTTPException(400, "Adresse gehört nicht zu diesem Pool")
    with ipam.alloc_lock:
        if db.scalar(select(IPAddress).where(IPAddress.pool_id == pool.id, IPAddress.address == str(addr))):
            raise HTTPException(400, "Adresse ist bereits vergeben oder reserviert")
        db.add(IPAddress(pool_id=pool.id, address=str(addr), reserved=True, note=body.note))
        audit(db, user, "ip-reserve", f"{pool.name} {addr}", client_ip(request))
        db.commit()
    return {"ok": True}


@router.delete("/{pool_id}/addresses/{address_id}")
def release_address(pool_id: int, address_id: int, request: Request, user: User = Depends(require("pools.manage")),
                    db: Session = Depends(get_db)):
    addr = db.get(IPAddress, address_id)
    if not addr or addr.pool_id != pool_id:
        raise HTTPException(404, "Adresse nicht gefunden")
    if addr.guest_id:
        raise HTTPException(400, "Adresse ist einem Server zugewiesen – Server zuerst löschen")
    audit(db, user, "ip-release", addr.address, client_ip(request))
    db.delete(addr)
    db.commit()
    return {"ok": True}
