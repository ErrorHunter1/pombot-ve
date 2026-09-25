"""Firewall pro Gast mit nftables (Tabelle `bridge pombot`).

Der Datenverkehr wird auf der Bridge anhand der MAC-Adresse des Gastes gefiltert. Das funktioniert
für VMs und Container gleichermaßen, ohne dass Interface-Namen bekannt sein müssen.
Zusätzlich verhindert der Spoofing-Schutz, dass ein Gast fremde IP-Adressen als Absender nutzt.
Die Konfiguration jedes Gastes liegt als JSON unter /var/lib/pombot/firewall/ und wird beim Start
des Agents und bei jeder Änderung komplett neu geladen (atomar per `nft -f`).
"""
import ipaddress
import json
import logging
import re
import tempfile
import threading
from pathlib import Path

from .config import DATA_DIR
from .util import CmdError, check_name, run

log = logging.getLogger("pombot.firewall")
FW_DIR = DATA_DIR / "firewall"
TABLE = "pombot"
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
PORT_RE = re.compile(r"^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$")
_LOCK = threading.Lock()


def _ports(raw: str) -> str:
    raw = raw.replace(" ", "")
    if not PORT_RE.match(raw):
        raise CmdError(f"Ungültige Portangabe: {raw}")
    for part in raw.split(","):
        bounds = [int(x) for x in part.split("-")]
        if any(not 1 <= p <= 65535 for p in bounds) or (len(bounds) == 2 and bounds[0] > bounds[1]):
            raise CmdError(f"Ungültiger Port: {part}")
    return raw


def validate(cfg: dict) -> dict:
    mac = str(cfg.get("mac", "")).lower()
    if not MAC_RE.match(mac):
        raise CmdError("Ungültige MAC-Adresse")
    ips = [str(ipaddress.ip_address(ip)) for ip in cfg.get("ips", [])]
    clean = {
        "enabled": bool(cfg.get("enabled")),
        "mac": mac,
        "ips": ips,
        "policy_in": "drop" if cfg.get("policy_in") == "drop" else "accept",
        "policy_out": "drop" if cfg.get("policy_out") == "drop" else "accept",
        "antispoof": bool(cfg.get("antispoof", True)),
        "rules": [],
    }
    for rule in cfg.get("rules", [])[:200]:
        r = {
            "direction": "out" if rule.get("direction") == "out" else "in",
            "action": "drop" if rule.get("action") == "drop" else "accept",
            "protocol": rule.get("protocol", "tcp"),
            "port": str(rule.get("port") or "").strip(),
            "source": str(rule.get("source") or "").strip(),
            "enabled": rule.get("enabled", True) is not False,
        }
        if r["protocol"] not in ("tcp", "udp", "icmp", "any"):
            raise CmdError("Protokoll muss tcp, udp, icmp oder any sein")
        if r["port"]:
            if r["protocol"] not in ("tcp", "udp"):
                raise CmdError("Ports gibt es nur bei TCP und UDP")
            r["port"] = _ports(r["port"])
        if r["source"]:
            r["source"] = str(ipaddress.ip_network(r["source"], strict=False))
        clean["rules"].append(r)
    return clean


def _rule_nft(rule: dict) -> str:
    parts = []
    if rule["source"]:
        net = ipaddress.ip_network(rule["source"])
        fam = "ip" if net.version == 4 else "ip6"
        field = "saddr" if rule["direction"] == "in" else "daddr"
        parts.append(f"{fam} {field} {net}")
    if rule["protocol"] == "icmp":
        parts.append("meta l4proto { icmp, ipv6-icmp }")
    elif rule["protocol"] in ("tcp", "udp"):
        if rule["port"]:
            parts.append(f"{rule['protocol']} dport {{ {rule['port'].replace(',', ', ')} }}")
        else:
            parts.append(f"meta l4proto {rule['protocol']}")
    parts.append(rule["action"])
    return " ".join(parts)


def build_ruleset(configs: dict[str, dict]) -> str:
    lines = [f"table bridge {TABLE}", f"delete table bridge {TABLE}", f"table bridge {TABLE} {{"]
    jumps = []
    for name, cfg in sorted(configs.items()):
        if not cfg["enabled"] and not cfg["antispoof"]:
            continue
        mac = cfg["mac"]
        v4 = [ip for ip in cfg["ips"] if ipaddress.ip_address(ip).version == 4]
        v6 = [ip for ip in cfg["ips"] if ipaddress.ip_address(ip).version == 6]
        if cfg["enabled"]:
            # Eingehend: Antworten erlauben, dann Regeln, dann Standardaktion
            lines += [f"  chain in_{name} {{",
                      "    ct state established,related accept",
                      "    ct state invalid drop",
                      "    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, packet-too-big } accept"]
            lines += [f"    {_rule_nft(r)}" for r in cfg["rules"] if r["direction"] == "in" and r["enabled"]]
            lines += [f"    {cfg['policy_in']}", "  }"]
            jumps.append(f"    ether daddr {mac} jump in_{name}")
        # Ausgehend: Spoofing-Schutz (auch ohne aktive Firewall), danach Regeln
        lines += [f"  chain out_{name} {{", "    ether type arp accept"]
        if cfg["antispoof"]:
            if v4:
                lines.append(f"    ether type ip ip saddr != {{ {', '.join(v4)} }} drop")
            if v6:
                lines.append(f"    ether type ip6 ip6 saddr != {{ {', '.join(v6)}, fe80::/10 }} drop")
        if cfg["enabled"]:
            lines += ["    ct state established,related accept",
                      "    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit, packet-too-big } accept"]
            lines += [f"    {_rule_nft(r)}" for r in cfg["rules"] if r["direction"] == "out" and r["enabled"]]
            lines.append(f"    {cfg['policy_out']}")
        lines.append("  }")
        jumps.append(f"    ether saddr {mac} jump out_{name}")
    lines += ["  chain forward {", "    type filter hook forward priority 0; policy accept;", *jumps, "  }", "}"]
    return "\n".join(lines) + "\n"


def _load_all() -> dict[str, dict]:
    configs = {}
    if FW_DIR.exists():
        for f in FW_DIR.glob("pv*.json"):
            try:
                configs[f.stem] = validate(json.loads(f.read_text()))
            except (ValueError, CmdError) as exc:
                log.error("Firewall-Konfiguration %s ungültig: %s", f.name, exc)
    return configs


def apply() -> None:
    with _LOCK:
        ruleset = build_ruleset(_load_all())
        with tempfile.NamedTemporaryFile("w", suffix=".nft", delete=False) as fh:
            fh.write(ruleset)
            path = fh.name
        try:
            run(["nft", "-f", path], timeout=30)
        finally:
            Path(path).unlink(missing_ok=True)


def ensure_loaded() -> None:
    """Lädt die Regeln, falls Konfigurationen existieren, die Tabelle aber fehlt."""
    if not FW_DIR.exists() or not any(FW_DIR.glob("pv*.json")):
        return
    try:
        run(["nft", "list", "table", "bridge", TABLE], timeout=15)
    except CmdError:
        log.warning("nftables-Tabelle fehlt – lade Firewall-Regeln neu")
        apply()


def get(name: str) -> dict | None:
    f = FW_DIR / f"{check_name(name)}.json"
    return json.loads(f.read_text()) if f.exists() else None


def put(name: str, cfg: dict) -> dict:
    clean = validate(cfg)
    FW_DIR.mkdir(parents=True, exist_ok=True)
    f = FW_DIR / f"{check_name(name)}.json"
    old = f.read_text() if f.exists() else None
    f.write_text(json.dumps(clean, indent=2))
    try:
        apply()
    except CmdError:
        # Bei Fehler alten Zustand wiederherstellen, damit die restlichen Regeln aktiv bleiben
        if old is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(old)
        raise
    return clean


def remove(name: str) -> None:
    f = FW_DIR / f"{check_name(name)}.json"
    if f.exists():
        f.unlink()
        try:
            apply()
        except CmdError as exc:
            log.error("Firewall nach Löschen nicht neu geladen: %s", exc)
