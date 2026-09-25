"""Hochverfügbarkeit (HA) für Server auf gemeinsamem Speicher.

Schutz vor „Split-Brain“ (derselbe Server läuft auf zwei Nodes und zerstört seine Festplatte) über
Leases auf dem gemeinsamen Speicher statt über die Verbindung zum Panel:

- Solange ein HA-Server hier läuft, erneuert dieser Node alle 5 s die Lease-Datei im Server-Verzeichnis
  (<speicher>/guests/<name>/.pombot-lease.json).
- Kann der Node den Speicher 30 s lang nicht beschreiben, stoppt er seine HA-Server selbst (Self-Fencing).
- Ein HA-Server startet nur, wenn keine frische Lease eines anderen Nodes existiert (älter als 60 s = frei).
- Fällt ein Node aus, übernimmt ein anderer Node den Server erst, wenn dessen Lease abgelaufen ist.
Ein Ausfall des Panels allein löst nichts aus – die Nodes erneuern ihre Leases weiter.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

from .config import DATA_DIR, GUEST_DIR, LXC_PATH
from .util import CmdError, check_name, run

log = logging.getLogger("pombot.ha")
STATE = DATA_DIR / "ha.json"
LEASE = ".pombot-lease.json"
RENEW = 5
FENCE_AFTER = 30
LEASE_TTL = 60
_lock = threading.Lock()
_write_fail_since: dict[str, float] = {}


def node_id() -> str:
    try:
        return Path("/etc/machine-id").read_text().strip() or os.uname().nodename
    except OSError:
        return os.uname().nodename


def ha_guests() -> set[str]:
    try:
        return set(json.loads(STATE.read_text()))
    except (OSError, ValueError):
        return set()


def _lease_path(name: str) -> Path | None:
    local = GUEST_DIR / check_name(name)
    return local / LEASE if local.is_symlink() and local.exists() else None


def read_lease(name: str) -> dict | None:
    p = _lease_path(name)
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def lease_holder(name: str) -> str | None:
    """Node-ID, die den Server gerade hält (frische Lease eines anderen Nodes), sonst None."""
    lease = read_lease(name)
    if lease and lease.get("node") != node_id() and time.time() - float(lease.get("ts", 0)) < LEASE_TTL:
        return lease.get("hostname") or lease.get("node")
    return None


def write_lease(name: str) -> None:
    p = _lease_path(name)
    if not p:
        return
    tmp = p.with_name(LEASE + f".{node_id()[:8]}.tmp")
    tmp.write_text(json.dumps({"node": node_id(), "hostname": os.uname().nodename, "ts": time.time()}))
    os.replace(tmp, p)


def check_start(name: str) -> None:
    holder = lease_holder(name)
    if holder:
        raise CmdError(f"Der Server läuft (laut Lease) noch auf Node {holder} – Start hier verweigert")
    if name in ha_guests():
        write_lease(name)


def _type(name: str) -> str | None:
    if (LXC_PATH / name / "config").exists():
        return "lxc"
    if run(["virsh", "dominfo", name], check=False).strip():
        return "kvm"
    return None


def _running(name: str, gtype: str) -> bool:
    if gtype == "kvm":
        return run(["virsh", "domstate", name], check=False).strip() == "running"
    return "RUNNING" in run(["lxc-info", "-n", name, "-s", "-H"], check=False)


def _hard_stop(name: str, gtype: str) -> None:
    if gtype == "kvm":
        run(["virsh", "destroy", name], check=False)
    else:
        run(["lxc-stop", "-n", name, "-k"], check=False)


def set_autostart(name: str, gtype: str, enabled: bool) -> None:
    if gtype == "kvm":
        run(["virsh", "autostart", name] + ([] if enabled else ["--disable"]), check=False)
    else:
        from . import lxc
        lxc.set_config(name, "lxc.start.auto", "1" if enabled else "0")


def configure(names: list[str]) -> dict:
    """Vom Panel: welche Server auf diesem Node HA-geschützt sind. HA-Server starten nie automatisch
    beim Booten des Nodes – das entscheidet das Panel (sonst liefen sie evtl. doppelt)."""
    wanted = {check_name(n) for n in names}
    with _lock:
        before = ha_guests()
        STATE.write_text(json.dumps(sorted(wanted)))
    for name in wanted ^ before:
        gtype = _type(name)
        if gtype:
            set_autostart(name, gtype, name not in wanted)
    return {"ha": sorted(wanted)}


def tick() -> None:
    """Alle 5 s: Leases erneuern, bei dauerhaft unerreichbarem Speicher selbst abschalten."""
    now = time.time()
    for name in ha_guests():
        gtype = _type(name)
        if not gtype or not _running(name, gtype):
            _write_fail_since.pop(name, None)
            continue
        try:
            write_lease(name)
            _write_fail_since.pop(name, None)
        except OSError as exc:
            since = _write_fail_since.setdefault(name, now)
            if now - since > FENCE_AFTER:
                log.error("Self-Fencing: Speicher für %s seit %ds nicht beschreibbar (%s) – Server wird gestoppt",
                          name, int(now - since), exc)
                _hard_stop(name, gtype)
                _write_fail_since.pop(name, None)


def loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("HA-Fehler")
        time.sleep(RENEW)


def takeover(name: str, gtype: str, storage_id: str, protect: bool = True) -> dict:
    """Übernimmt einen Server vom Speicher (HA nach Ausfall oder Migration) – nur ohne gültige fremde Lease."""
    from . import storage
    check_name(name)
    target = storage.adopt_link(storage_id, name)
    holder = lease_holder(name)
    if holder:
        (GUEST_DIR / name).unlink(missing_ok=True)
        raise CmdError(f"Lease von Node {holder} ist noch gültig – Übernahme abgebrochen")
    try:
        register(name, gtype, target)
    except Exception:
        (GUEST_DIR / name).unlink(missing_ok=True)
        raise
    if protect:
        write_lease(name)
        configure(sorted(ha_guests() | {name}))
    return {"ok": True}


def register(name: str, gtype: str, target: Path) -> None:
    """Server aus dem Verzeichnis auf dem Speicher auf diesem Node registrieren (ohne Daten zu kopieren)."""
    if gtype == "kvm":
        if not run(["virsh", "dominfo", name], check=False).strip():
            run(["virsh", "define", target / "domain.xml"])
    else:
        link = LXC_PATH / name
        if link.is_symlink():
            link.unlink()
        if link.exists():
            raise CmdError(f"{link} existiert lokal – Konflikt")
        link.symlink_to(target / "lxc")


def release(name: str, stale: bool = False) -> None:
    """Server auf diesem Node abmelden, Daten auf dem Speicher bleiben (für Migration/nach Übernahme).
    stale=True: der Server wurde inzwischen von einem anderen Node übernommen – dann weder domain.xml
    noch Lease auf dem Speicher anfassen (die gehören jetzt dem neuen Node)."""
    check_name(name)
    local = GUEST_DIR / name
    if not local.is_symlink():
        raise CmdError("Server liegt nicht auf gemeinsamem Speicher")
    gtype = _type(name)
    if gtype and _running(name, gtype):
        raise CmdError("Server läuft noch – erst stoppen")
    if gtype == "kvm":
        if not stale:
            (local / "domain.xml").write_text(run(["virsh", "dumpxml", "--inactive", name]))
        run(["virsh", "undefine", name, "--managed-save", "--snapshots-metadata"])
    link = LXC_PATH / name
    if link.is_symlink():
        link.unlink()
    if not stale:
        lease = read_lease(name)
        if not lease or lease.get("node") == node_id():
            (local / LEASE).unlink(missing_ok=True)  # Lease freigeben, damit der Ziel-Node sofort übernehmen darf
    local.unlink()
    with _lock:
        names = ha_guests() - {name}
        STATE.write_text(json.dumps(sorted(names)))
