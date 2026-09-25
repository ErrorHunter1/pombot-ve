"""Geroutetes Netzwerk für Zusatz-IPs ohne eigene MAC-Adresse (z. B. skrime, Hetzner, OVH).

Der Host nimmt die Zusatz-IPs mit seiner eigenen MAC entgegen (Proxy-ARP) und leitet sie über die
interne Bridge `pbr0` an die Gäste weiter. Jeder Gast bekommt seine IP als /32, Gateway ist die
Haupt-IP des Hosts (on-link). Beim Hoster muss nichts eingerichtet werden.

Aufruf als Modul (`python -m pombot_agent.routed`) stellt Bridge und Routen nach einem Neustart
wieder her – der Dienst pombot-network.service macht das vor libvirt und LXC.
"""
import ipaddress
import json
import logging
import threading
from pathlib import Path

from .config import DATA_DIR
from .util import CmdError, check_name, run

log = logging.getLogger("pombot.routed")
BRIDGE = "pbr0"
NET_DIR = DATA_DIR / "routed"
SYSCTL_FILE = Path("/etc/sysctl.d/90-pombot-routed.conf")
_LOCK = threading.Lock()


def uplink() -> tuple[str, str]:
    """(Haupt-IPv4, Netzwerkkarte) anhand der Default-Route."""
    parts = run(["ip", "-4", "route", "get", "1.1.1.1"], timeout=10).split()
    try:
        return parts[parts.index("src") + 1], parts[parts.index("dev") + 1]
    except (ValueError, IndexError):
        raise CmdError("Haupt-IP des Hosts nicht ermittelbar (keine IPv4-Default-Route?)")


def _sysctl(key: str, value: str) -> None:
    run(["sysctl", "-qw", f"{key}={value}"], check=False)


def ensure_bridge() -> None:
    main_ip, dev = uplink()
    if dev == BRIDGE:
        raise CmdError("Die Default-Route zeigt auf pbr0 – Netzwerkkonfiguration prüfen")
    if not Path(f"/sys/class/net/{BRIDGE}").exists():
        run(["ip", "link", "add", BRIDGE, "type", "bridge"])
        run(["ip", "link", "set", BRIDGE, "type", "bridge", "stp_state", "0", "forward_delay", "0"], check=False)
    run(["ip", "link", "set", BRIDGE, "up"])
    run(["ip", "addr", "replace", f"{main_ip}/32", "dev", BRIDGE])
    settings = {
        "net.ipv4.ip_forward": "1",
        f"net.ipv4.conf.{dev}.proxy_arp": "1",
        f"net.ipv4.conf.{BRIDGE}.proxy_arp": "1",
    }
    for key, value in settings.items():
        _sysctl(key, value)
    content = "# Von PomBot VE (geroutete Zusatz-IPs)\n" + "".join(f"{k} = {v}\n" for k, v in settings.items())
    try:
        if not SYSCTL_FILE.exists() or SYSCTL_FILE.read_text() != content:
            SYSCTL_FILE.write_text(content)
    except OSError as exc:
        log.warning("Konnte %s nicht schreiben: %s", SYSCTL_FILE, exc)


SKIP_IFACES = ("lo", BRIDGE, "virbr", "lxcbr", "docker", "veth", "vnet", "tap", "br-")


def _host_addresses() -> list[dict]:
    data = json.loads(run(["ip", "-j", "-4", "addr", "show"], timeout=10) or "[]")
    result = []
    for ifc in data:
        for a in ifc.get("addr_info", []):
            if a.get("family") == "inet":
                result.append({"interface": ifc["ifname"], "address": a["local"], "prefix": a["prefixlen"],
                               "scope": a.get("scope")})
    return result


def network_info() -> dict:
    """Alle IPv4-Adressen und Gateways des Hosts – Grundlage für die automatische Erkennung im Panel."""
    main_ip, dev = uplink()
    routes = json.loads(run(["ip", "-j", "-4", "route", "show"], timeout=10) or "[]")
    gateways = [{"gateway": r["gateway"], "interface": r.get("dev"), "default": r.get("dst") == "default"}
                for r in routes if r.get("gateway")]
    default_gw = next((g["gateway"] for g in gateways if g["default"]), None)
    routed_ips = set()
    if NET_DIR.exists():
        for f in NET_DIR.glob("pv*.json"):
            try:
                routed_ips.update(json.loads(f.read_text()).get("ips", []))
            except ValueError:
                pass
    addresses = []
    for a in _host_addresses():
        if a["scope"] != "global" or a["interface"].startswith(SKIP_IFACES) and a["interface"] != dev:
            continue
        ip = ipaddress.ip_address(a["address"])
        addresses.append({**a, "main": a["address"] == main_ip, "public": ip.is_global,
                          "network": str(ipaddress.ip_interface(f"{ip}/{a['prefix']}").network)})
    for ip in sorted(routed_ips):
        addresses.append({"interface": BRIDGE, "address": ip, "prefix": 32, "scope": "global", "main": False,
                          "public": ipaddress.ip_address(ip).is_global, "network": f"{ip}/32", "routed": True})
    return {"main_ipv4": main_ip, "uplink": dev, "default_gateway": default_gw, "gateways": gateways,
            "addresses": addresses}


def _release_from_host(ips: list[str], job=None) -> None:
    """Eine Zusatz-IP darf nicht direkt auf einer Netzwerkkarte des Hosts liegen – sonst beantwortet der
    Host sie selbst und leitet nichts an den Gast weiter. Die Haupt-IP bleibt immer unangetastet."""
    main_ip, _ = uplink()
    wanted = set(ips) - {main_ip}
    for a in _host_addresses():
        if a["address"] in wanted and a["interface"] != BRIDGE:
            run(["ip", "addr", "del", f"{a['address']}/{a['prefix']}", "dev", a["interface"]], check=False)
            msg = (f"{a['address']} war direkt auf {a['interface']} eingetragen und wurde dort entfernt "
                   "(wird jetzt an den Gast weitergeleitet).")
            log.info(msg)
            if job:
                job.write(msg)


def _routes(ips: list[str], action: str) -> None:
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if addr.version == 4:
            run(["ip", "route", action, f"{addr}/32", "dev", BRIDGE], check=action == "replace")


def set_guest(name: str, ips: list[str], job=None) -> None:
    """Bridge einrichten und die IPs des Gastes auf pbr0 routen (vor dem Start des Gastes)."""
    ips = [str(ipaddress.ip_address(ip)) for ip in ips]
    main_ip, _ = uplink()
    if main_ip in ips:
        raise CmdError(f"{main_ip} ist die Haupt-IP des Nodes und kann keinem Server gegeben werden")
    with _LOCK:
        ensure_bridge()
        _release_from_host(ips, job)
        NET_DIR.mkdir(parents=True, exist_ok=True)
        (NET_DIR / f"{check_name(name)}.json").write_text(json.dumps({"ips": ips}))
        _routes(ips, "replace")
    if job:
        main_ip, dev = uplink()
        job.write(f"Geroutetes Netz: {', '.join(ips)} über {BRIDGE} (Gateway {main_ip}, Proxy-ARP auf {dev})")


def remove_guest(name: str) -> None:
    f = NET_DIR / f"{check_name(name)}.json"
    if not f.exists():
        return
    with _LOCK:
        try:
            _routes(json.loads(f.read_text()).get("ips", []), "del")
        except (ValueError, CmdError) as exc:
            log.warning("Routen für %s nicht entfernt: %s", name, exc)
        f.unlink(missing_ok=True)


def ensure_all() -> None:
    """Stellt Bridge und alle Routen wieder her (nach Neustart oder Netzwerk-Neustart)."""
    if not NET_DIR.exists():
        return
    files = list(NET_DIR.glob("pv*.json"))
    if not files:
        return
    with _LOCK:
        ensure_bridge()
        for f in files:
            try:
                ips = json.loads(f.read_text()).get("ips", [])
                _release_from_host(ips)
                _routes(ips, "replace")
            except (ValueError, CmdError) as exc:
                log.error("Routen aus %s nicht gesetzt: %s", f.name, exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_all()
