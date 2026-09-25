"""IP-Adressverwaltung: freie Adressen aus Pools vergeben.

Ein Pool liefert Adressen entweder aus einem Netz/Bereich (CIDR, optional von–bis) oder aus einer
Liste einzelner Adressen (z. B. 7 gebuchte Zusatz-IPs). Modus:
  bridged – Gäste hängen direkt am Netz des Hosters (braucht meist eigene MAC-Adressen)
  routed  – der Node routet die IPs an die Gäste weiter (/32, Gateway = Haupt-IP des Nodes)
"""
import ipaddress
import threading

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import IPAddress, IPPool, Node

ROUTED_BRIDGE = "pbr0"
# Vergabe serialisieren, damit zwei gleichzeitige Bestellungen nie dieselbe IP bekommen.
alloc_lock = threading.Lock()
MAX_SCAN = 1_000_000


def parse_list(raw: str) -> list[str]:
    """Adressen aus Freitext: getrennt durch Zeilen, Kommas oder Leerzeichen; `a-b` für kleine Bereiche."""
    result: list[str] = []
    for token in raw.replace(",", " ").replace(";", " ").split():
        token = token.strip()
        if not token:
            continue
        try:
            if "-" in token:
                start_s, end_s = token.split("-", 1)
                start = ipaddress.ip_address(start_s)
                end = ipaddress.ip_address(end_s) if "." in end_s or ":" in end_s else \
                    ipaddress.ip_address(".".join(start_s.split(".")[:3] + [end_s]))
                if end < start or int(end) - int(start) > 4096:
                    raise ValueError
                result += [str(ipaddress.ip_address(i)) for i in range(int(start), int(end) + 1)]
            elif "/" in token:
                result.append(str(ipaddress.ip_interface(token).ip))
            else:
                result.append(str(ipaddress.ip_address(token)))
        except ValueError:
            raise HTTPException(400, f"Ungültige Adresse in der Liste: {token}")
    seen, unique = set(), []
    for ip in result:
        if ip not in seen:
            seen.add(ip)
            unique.append(ip)
    return unique


def pool_list(pool: IPPool) -> list[str]:
    return parse_list(pool.address_list) if pool.address_list else []


def validate_pool(mode: str, network: str, gateway: str | None, range_start: str | None, range_end: str | None,
                  address_list: str) -> str:
    """Prüft die Pool-Angaben und liefert das normalisierte Netz (bei reinen Listen ggf. leer)."""
    addresses = parse_list(address_list) if address_list.strip() else []
    net = None
    if network.strip():
        try:
            net = ipaddress.ip_network(network.strip(), strict=False)
        except ValueError:
            raise HTTPException(400, "Ungültiges Netz (Beispiel: 203.0.113.0/24)")
    if not addresses and not net:
        raise HTTPException(400, "Bitte ein Netz (CIDR) oder eine Liste einzelner Adressen angeben")
    versions = {ipaddress.ip_address(a).version for a in addresses} | ({net.version} if net else set())
    if len(versions) > 1:
        raise HTTPException(400, "IPv4 und IPv6 bitte in getrennten Pools anlegen")
    if mode == "routed":
        if versions != {4}:
            raise HTTPException(400, "Der geroutete Modus unterstützt derzeit nur IPv4")
        return str(net) if net else ""
    # Bridge-Modus: Präfix und Gateway kommen aus dem Netz
    if not net:
        raise HTTPException(400, "Im Bridge-Modus wird das Netz (CIDR) benötigt, zu dem die Adressen gehören, "
                                 "z. B. 203.0.113.0/26 – oder den gerouteten Modus wählen")
    try:
        if gateway and ipaddress.ip_address(gateway).version != net.version:
            raise HTTPException(400, "Gateway passt nicht zur IP-Version des Netzes")
        for value in (range_start, range_end):
            if value and ipaddress.ip_address(value) not in net:
                raise HTTPException(400, f"{value} liegt nicht im Netz {net}")
    except ValueError:
        raise HTTPException(400, "Ungültige IP-Adresse")
    if range_start and range_end and ipaddress.ip_address(range_start) > ipaddress.ip_address(range_end):
        raise HTTPException(400, "Bereichsanfang liegt hinter dem Bereichsende")
    for a in addresses:
        if ipaddress.ip_address(a) not in net:
            raise HTTPException(400, f"{a} liegt nicht im Netz {net} – im Bridge-Modus müssen alle Adressen "
                                     "im angegebenen Netz liegen")
    return str(net)


def _bounds(pool: IPPool):
    net = ipaddress.ip_network(pool.network, strict=False)
    start = ipaddress.ip_address(pool.range_start) if pool.range_start else \
        net.network_address + (1 if net.num_addresses > 2 else 0)
    if pool.range_end:
        end = ipaddress.ip_address(pool.range_end)
    elif net.version == 4 and net.num_addresses > 2:
        end = net.broadcast_address - 1
    else:
        end = net.broadcast_address
    return net, start, end


def _candidates(pool: IPPool):
    """Alle vergebbaren Adressen des Pools (Generator), ohne Gateway."""
    gateway = str(ipaddress.ip_address(pool.gateway)) if pool.gateway else None
    if pool.address_list:
        for ip in pool_list(pool):
            if ip != gateway:
                yield ip
        return
    _, start, end = _bounds(pool)
    cur, scanned = start, 0
    while cur <= end and scanned < MAX_SCAN:
        if str(cur) != gateway:
            yield str(cur)
        cur += 1
        scanned += 1


def pool_size(pool: IPPool) -> int:
    if pool.address_list:
        return sum(1 for _ in _candidates(pool))
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
    used = set(db.scalars(select(IPAddress.address).where(IPAddress.pool_id == pool.id)).all())
    return next((ip for ip in _candidates(pool) if ip not in used), None)


def contains(pool: IPPool, address: str) -> bool:
    if pool.address_list:
        return address in pool_list(pool)
    return ipaddress.ip_address(address) in ipaddress.ip_network(pool.network, strict=False)


def allocate(db: Session, pool: IPPool, guest_id: int | None = None) -> IPAddress | None:
    """Muss innerhalb von `alloc_lock` aufgerufen werden."""
    address = free_address(db, pool)
    if not address:
        return None
    ip = IPAddress(pool_id=pool.id, address=address, guest_id=guest_id)
    db.add(ip)
    db.flush()
    return ip


def is_routed(pool: IPPool) -> bool:
    return pool.mode == "routed"


def ip_spec(ip: IPAddress, node: Node) -> dict:
    """Adresse, Präfix und Gateway so, wie sie im Gast eingetragen werden."""
    pool = ip.pool
    version = ipaddress.ip_address(ip.address).version
    if is_routed(pool):
        gateway = node.info.get("main_ipv4")
        if not gateway:
            raise HTTPException(400, f"Haupt-IP von Node {node.name} unbekannt – bitte unter Nodes "
                                     "„Infos neu laden“ klicken")
        return {"address": ip.address, "prefix": 32, "gateway": gateway, "version": version}
    return {"address": ip.address, "prefix": prefix_of(pool), "gateway": pool.gateway or None, "version": version}


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
    if pool.network:
        return ipaddress.ip_network(pool.network, strict=False).version
    addresses = pool_list(pool)
    return ipaddress.ip_address(addresses[0]).version if addresses else 4


def prefix_of(pool: IPPool) -> int:
    if is_routed(pool):
        return 32 if version_of(pool) == 4 else 128
    return ipaddress.ip_network(pool.network, strict=False).prefixlen
