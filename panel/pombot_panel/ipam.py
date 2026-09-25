"""IP-Adressverwaltung: freie Adressen aus Pools vergeben."""
import ipaddress
import threading

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import IPAddress, IPPool

# Vergabe serialisieren, damit zwei gleichzeitige Bestellungen nie dieselbe IP bekommen.
alloc_lock = threading.Lock()
MAX_SCAN = 1_000_000


def validate_pool(network: str, gateway: str | None, range_start: str | None, range_end: str | None):
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        raise HTTPException(400, "Ungültiges Netz (Beispiel: 203.0.113.0/24)")
    try:
        if gateway:
            gw = ipaddress.ip_address(gateway)
            if gw.version != net.version:
                raise HTTPException(400, "Gateway passt nicht zur IP-Version des Netzes")
        for value in (range_start, range_end):
            if value and ipaddress.ip_address(value) not in net:
                raise HTTPException(400, f"{value} liegt nicht im Netz {net}")
    except ValueError:
        raise HTTPException(400, "Ungültige IP-Adresse")
    if range_start and range_end and ipaddress.ip_address(range_start) > ipaddress.ip_address(range_end):
        raise HTTPException(400, "Bereichsanfang liegt hinter dem Bereichsende")
    return net


def _bounds(pool: IPPool):
    net = ipaddress.ip_network(pool.network, strict=False)
    if pool.range_start:
        start = ipaddress.ip_address(pool.range_start)
    else:
        start = net.network_address + (1 if net.num_addresses > 2 else 0)
    if pool.range_end:
        end = ipaddress.ip_address(pool.range_end)
    elif net.version == 4 and net.num_addresses > 2:
        end = net.broadcast_address - 1
    else:
        end = net.broadcast_address
    return net, start, end


def pool_size(pool: IPPool) -> int:
    _, start, end = _bounds(pool)
    size = int(end) - int(start) + 1
    if pool.gateway:
        gw = ipaddress.ip_address(pool.gateway)
        if gw.version == start.version and start <= gw <= end:
            size -= 1
    return max(size, 0)


def pool_used(db: Session, pool: IPPool) -> int:
    return len(db.scalars(select(IPAddress.id).where(IPAddress.pool_id == pool.id)).all())


def free_address(db: Session, pool: IPPool) -> str | None:
    net, start, end = _bounds(pool)
    used = set(db.scalars(select(IPAddress.address).where(IPAddress.pool_id == pool.id)).all())
    if pool.gateway:
        used.add(str(ipaddress.ip_address(pool.gateway)))
    cur, scanned = start, 0
    while cur <= end and scanned < MAX_SCAN:
        if str(cur) not in used:
            return str(cur)
        cur += 1
        scanned += 1
    return None


def allocate(db: Session, pool: IPPool, guest_id: int | None = None) -> IPAddress | None:
    """Muss innerhalb von `alloc_lock` aufgerufen werden."""
    address = free_address(db, pool)
    if not address:
        return None
    ip = IPAddress(pool_id=pool.id, address=address, guest_id=guest_id)
    db.add(ip)
    db.flush()
    return ip


def ip_spec(ip: IPAddress) -> dict:
    pool = ip.pool
    net = ipaddress.ip_network(pool.network, strict=False)
    return {"address": ip.address, "prefix": net.prefixlen, "gateway": pool.gateway or None,
            "version": net.version}


def pool_dns(pool: IPPool | None) -> list[str]:
    raw = (pool.dns if pool and pool.dns else settings.default_dns)
    result = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if item:
            try:
                result.append(str(ipaddress.ip_address(item)))
            except ValueError:
                pass
    return result


def version_of(pool: IPPool) -> int:
    return ipaddress.ip_network(pool.network, strict=False).version


def prefix_of(pool: IPPool) -> int:
    return ipaddress.ip_network(pool.network, strict=False).prefixlen
