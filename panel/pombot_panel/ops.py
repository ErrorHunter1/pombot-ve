"""Geschäftslogik: Platzierung, Kontingente, Gast-Operationen, Node-Installation per SSH."""
import asyncio
import base64
import io
import json
import secrets
import tarfile
import time
from pathlib import Path

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ipam
from .agent_client import AgentClient, AgentError, cert_fingerprint
from .config import settings
from .db import session_scope
from .models import Guest, IPAddress, IPPool, Node, Setting, Template, User
from .tasks import agent_job, task_log

MIB = 1 << 20


# ---------------------------------------------------------------- Kontingente

def usage(db: Session, user: User, exclude_guest: int | None = None) -> dict:
    q = select(Guest).where(Guest.owner_id == user.id)
    guests = [g for g in db.scalars(q) if g.id != exclude_guest]
    ips = db.scalar(select(func.count(IPAddress.id)).join(Guest, IPAddress.guest_id == Guest.id)
                    .where(Guest.owner_id == user.id, Guest.id != (exclude_guest or -1))) or 0
    return {"guests": len(guests), "cores": sum(g.cores for g in guests),
            "memory_mb": sum(g.memory_mb for g in guests), "disk_gb": sum(g.disk_gb for g in guests),
            "ips": ips}


def quota(user: User) -> dict:
    return {"guests": user.max_guests, "cores": user.max_cores, "memory_mb": user.max_memory_mb,
            "disk_gb": user.max_disk_gb, "ips": user.max_ips}


def check_quota(db: Session, user: User, guests=0, cores=0, memory_mb=0, disk_gb=0, ips=0,
                exclude_guest: int | None = None) -> None:
    if user.is_admin:
        return
    used = usage(db, user, exclude_guest)
    limits = quota(user)
    wanted = {"guests": guests, "cores": cores, "memory_mb": memory_mb, "disk_gb": disk_gb, "ips": ips}
    labels = {"guests": "Server", "cores": "CPU-Kerne", "memory_mb": "RAM (MB)", "disk_gb": "Speicher (GB)",
              "ips": "IP-Adressen"}
    for key, add in wanted.items():
        if add and used[key] + add > limits[key]:
            raise HTTPException(400, f"Kontingent überschritten: {labels[key]} – "
                                     f"belegt {used[key]}, angefragt {add}, erlaubt {limits[key]}")


# ---------------------------------------------------------------- Platzierung

def node_free_memory_mb(node: Node) -> int:
    total = node.info.get("memory_total", 0) // MIB
    committed = sum(g.memory_mb for g in node.guests)
    return total - committed


def node_supports(node: Node, gtype: str) -> bool:
    info = node.info
    return bool(info.get("libvirt")) if gtype == "kvm" else bool(info.get("lxc"))


def pool_fits_node(pool: IPPool, node: Node) -> bool:
    if pool.node_id and pool.node_id != node.id:
        return False
    if ipam.is_routed(pool):
        return True  # pbr0 legt der Agent bei Bedarf selbst an
    bridges = node.info.get("bridges")
    return not bridges or pool.bridge in bridges


def usable_pools(db: Session, user: User, node: Node, version: int) -> list[IPPool]:
    pools = []
    for pool in db.scalars(select(IPPool).order_by(IPPool.id)):
        if pool.admin_only and not user.is_admin:
            continue
        if ipam.version_of(pool) == version and pool_fits_node(pool, node):
            pools.append(pool)
    return pools


def resolve_placement(db: Session, user: User, gtype: str, node_choice, v4_choice, v6_choice):
    """Liefert (node, v4_pool|None, v6_pool|None). choice: 'auto' | 'none' | <id>"""
    if node_choice in (None, "", "auto"):
        nodes = [n for n in db.scalars(select(Node).where(Node.status == "online", Node.enabled.is_(True)))
                 if node_supports(n, gtype)]
        nodes.sort(key=node_free_memory_mb, reverse=True)
        if not nodes:
            raise HTTPException(400, "Kein passender Node online")
    else:
        node = db.get(Node, int(node_choice))
        if not node:
            raise HTTPException(400, "Node nicht gefunden")
        if not user.is_admin and not node.enabled:
            raise HTTPException(400, "Dieser Node nimmt keine neuen Server an")
        if node.status != "online":
            raise HTTPException(400, f"Node {node.name} ist nicht online")
        if not node_supports(node, gtype):
            raise HTTPException(400, f"Node {node.name} unterstützt {gtype.upper()} nicht")
        nodes = [node]

    def pick(choice, version, node):
        if choice in ("none", None, ""):
            return None
        if choice == "auto":
            for pool in usable_pools(db, user, node, version):
                if ipam.free_address(db, pool):
                    return pool
            raise LookupError
        pool = db.get(IPPool, int(choice))
        if not pool or (pool.admin_only and not user.is_admin):
            raise HTTPException(400, "IP-Pool nicht gefunden")
        if ipam.version_of(pool) != version:
            raise HTTPException(400, f"Pool {pool.name} ist kein IPv{version}-Pool")
        if not pool_fits_node(pool, node):
            raise LookupError
        return pool

    last_error = "Kein IP-Pool mit freien Adressen für diesen Node gefunden"
    for node in nodes:
        try:
            v4 = pick(v4_choice, 4, node)
            v6 = pick(v6_choice, 6, node)
        except LookupError:
            continue
        if v4 and v6 and ipam.is_routed(v4) != ipam.is_routed(v6):
            last_error = "IPv4- und IPv6-Pool müssen beide geroutet oder beide gebridged sein"
            continue
        if v4 and v6 and ipam.is_routed(v4):
            return node, v4, v6
        if v4 and v6 and v4.bridge != v6.bridge:
            last_error = "IPv4- und IPv6-Pool müssen dieselbe Bridge nutzen"
            continue
        return node, v4, v6
    raise HTTPException(400, last_error)


def default_bridge(node: Node) -> str:
    bridges = node.info.get("bridges") or []
    return "vmbr0" if "vmbr0" in bridges or not bridges else bridges[0]


def next_vmid(db: Session) -> int:
    """VMIDs werden nie wiederverwendet – sonst sähe ein neuer Server die Backups eines gelöschten."""
    counter = db.get(Setting, "next_vmid")
    highest = (db.scalar(select(func.max(Guest.vmid))) or 99) + 1
    vmid = max(100, highest, int(counter.value) if counter else 0)
    if counter:
        counter.value = str(vmid + 1)
    else:
        db.add(Setting(key="next_vmid", value=str(vmid + 1)))
    return vmid


def new_mac(db: Session) -> str:
    while True:
        mac = "52:54:00:" + ":".join(f"{secrets.randbelow(256):02x}" for _ in range(3))
        if not db.scalar(select(Guest.id).where(Guest.mac == mac)):
            return mac


# ---------------------------------------------------------------- Gast-Spezifikation

def build_spec(db: Session, guest: Guest, template: Template | None, password: str | None,
               ssh_keys: list[str], suffix: str = "1") -> dict:
    ips = sorted(guest.ips, key=lambda ip: ipam.version_of(ip.pool))
    pool = ips[0].pool if ips else None
    routed = bool(pool and ipam.is_routed(pool))
    spec = {
        "name": guest.agent_name,
        "type": guest.type,
        "hostname": guest.hostname,
        "cores": guest.cores,
        "memory_mb": guest.memory_mb,
        "disk_gb": guest.disk_gb,
        "bridge": ipam.ROUTED_BRIDGE if routed else pool.bridge if pool else default_bridge(guest.node),
        "routed": routed,
        "mac": guest.mac,
        "ips": [ipam.ip_spec(ip, guest.node) for ip in ips],
        "dns": ipam.pool_dns(pool),
        "password": password,
        "ssh_keys": ssh_keys,
        "instance_suffix": suffix,
    }
    if template is None:
        if not guest.iso_file:
            raise AgentError("Server hat weder Vorlage noch ISO")
        spec["iso_file"] = guest.iso_file
    elif template.type == "lxc":
        spec["lxc_dist"], spec["lxc_release"] = template.lxc_dist, template.lxc_release
    elif template.source == "iso":
        spec["iso_url"] = template.url
    else:
        spec["image_url"] = template.url
    return spec


def _set_guest(guest_id: int, **fields) -> None:
    with session_scope() as db:
        guest = db.get(Guest, guest_id)
        if guest:
            for key, value in fields.items():
                setattr(guest, key, value)


def _client_for_guest(guest_id: int) -> tuple[AgentClient, str]:
    with session_scope() as db:
        guest = db.get(Guest, guest_id)
        if not guest:
            raise AgentError("Server existiert nicht mehr")
        return AgentClient.for_node(guest.node), guest.agent_name


async def op_create(task_id: int, guest_id: int, password: str | None, ssh_keys: list[str]) -> None:
    with session_scope() as db:
        guest = db.get(Guest, guest_id)
        spec = build_spec(db, guest, guest.template, password, ssh_keys)
        client = AgentClient.for_node(guest.node)
        ips = ", ".join(ip["address"] for ip in spec["ips"]) or "DHCP"
    task_log(task_id, f"Node: {client.base} – IP: {ips} – Bridge: {spec['bridge']}")
    try:
        result = await agent_job(task_id, client, "POST", "/guests", spec)
    except Exception as exc:
        _set_guest(guest_id, status="error", error=str(exc))
        raise
    _set_guest(guest_id, status="ready", error=None, power=result.get("state", "unknown"))
    await _apply_firewall(task_id, guest_id)


async def _apply_firewall(task_id: int, guest_id: int) -> None:
    from .routers.extras import push_firewall
    try:
        await asyncio.to_thread(push_firewall, guest_id)
        task_log(task_id, "Firewall / Spoofing-Schutz aktiviert.")
    except Exception as exc:  # noqa: BLE001 - Server läuft trotzdem, nur Hinweis
        task_log(task_id, f"WARNUNG: Firewall konnte nicht gesetzt werden: {exc}")


async def op_reinstall(task_id: int, guest_id: int, password: str | None, ssh_keys: list[str]) -> None:
    client, name = _client_for_guest(guest_id)
    _set_guest(guest_id, status="busy")
    try:
        task_log(task_id, "Entferne bisherige Installation …")
        await agent_job(task_id, client, "DELETE", f"/guests/{name}")
        with session_scope() as db:
            guest = db.get(Guest, guest_id)
            spec = build_spec(db, guest, guest.template, password, ssh_keys, suffix=secrets.token_hex(4))
        result = await agent_job(task_id, client, "POST", "/guests", spec)
    except Exception as exc:
        _set_guest(guest_id, status="error", error=str(exc))
        raise
    _set_guest(guest_id, status="ready", error=None, power=result.get("state", "unknown"))
    await _apply_firewall(task_id, guest_id)


async def op_delete(task_id: int, guest_id: int, force: bool = False) -> None:
    client, name = _client_for_guest(guest_id)
    _set_guest(guest_id, status="deleting")
    try:
        await agent_job(task_id, client, "DELETE", f"/guests/{name}")
    except AgentError as exc:
        if not force:
            _set_guest(guest_id, status="error", error=str(exc))
            raise
        task_log(task_id, f"Agent-Fehler ignoriert (erzwungenes Löschen): {exc}")
    with session_scope() as db:
        guest = db.get(Guest, guest_id)
        if guest:
            for ip in list(guest.ips):
                db.delete(ip)
            db.delete(guest)
    task_log(task_id, "Server und IP-Zuweisungen entfernt.")


async def op_job(task_id: int, guest_id: int, method: str, path: str, payload=None, after=None) -> None:
    """Allgemeiner Agent-Job für einen Gast (Snapshot, Backup, Resize, Restore …)."""
    client, name = _client_for_guest(guest_id)
    _set_guest(guest_id, status="busy")
    try:
        result = await agent_job(task_id, client, method, path.replace("{name}", name), payload)
    finally:
        _set_guest(guest_id, status="ready")
    if "state" in result:
        _set_guest(guest_id, power=result["state"])
    if after:
        after(result)


async def op_backup(task_id: int, guest_id: int, target: dict | None, sub: str, keep_local: bool = False,
                    auto: bool = False, keep: int | None = None) -> dict:
    """Backup erstellen, optional zu einem externen Speicher hochladen und alte automatische Backups
    über die Aufbewahrungsanzahl hinaus löschen (am jeweiligen Speicherort)."""
    client, name = _client_for_guest(guest_id)
    _set_guest(guest_id, status="busy")
    try:
        result = await agent_job(task_id, client, "POST", f"/guests/{name}/backups" + ("?auto=true" if auto else ""))
        if target:
            await agent_job(task_id, client, "POST", "/remote/upload",
                            {"target": target, "file": result["file"], "sub": sub, "delete_local": not keep_local})
    finally:
        _set_guest(guest_id, status="ready")
    if keep and auto:
        if target:
            files = await client.arequest("POST", "/remote/list", {"target": target, "sub": sub}, timeout=120)
            autos = [f for f in files if f.get("auto")]
            for old in autos[keep:]:
                await client.arequest("POST", "/remote/delete", {"target": target, "sub": sub, "file": old["file"]})
                task_log(task_id, f"Altes automatisches Backup gelöscht ({target.get('name')}): {old['file']}")
        else:
            backups = await client.arequest("GET", f"/backups?name={name}")
            autos = sorted((b for b in backups if b.get("auto")), key=lambda b: b["created"], reverse=True)
            for old in autos[keep:]:
                await client.arequest("DELETE", f"/backups/{old['file']}")
                task_log(task_id, f"Altes automatisches Backup gelöscht: {old['file']}")
        task_log(task_id, f"Aufbewahrt: höchstens {keep} automatische Backups.")
    return result


async def op_restore(task_id: int, guest_id: int, filename: str, target: dict | None, sub: str,
                     already_local: bool = True) -> None:
    """Wiederherstellen – bei externem Speicher vorher auf den Node laden und danach wieder aufräumen."""
    client, name = _client_for_guest(guest_id)
    _set_guest(guest_id, status="busy")
    fetched = False
    try:
        if target and not already_local:
            await agent_job(task_id, client, "POST", "/remote/fetch", {"target": target, "sub": sub, "file": filename})
            fetched = True
        result = await agent_job(task_id, client, "POST", f"/backups/{filename}/restore")
        if "state" in result:
            _set_guest(guest_id, power=result["state"])
    finally:
        _set_guest(guest_id, status="ready")
        if fetched:
            try:
                await client.arequest("DELETE", f"/backups/{filename}")
                task_log(task_id, "Heruntergeladene Kopie wieder gelöscht.")
            except AgentError:
                pass


async def _wait_stopped(client: AgentClient, name: str, task_id: int, timeout: int = 120) -> None:
    await client.arequest("POST", f"/guests/{name}/action", {"action": "shutdown"}, timeout=150)
    for _ in range(timeout // 3):
        st = await client.arequest("GET", f"/guests/{name}", timeout=30)
        if st.get("state") == "stopped":
            return
        await asyncio.sleep(3)
    task_log(task_id, "Server reagiert nicht auf Herunterfahren – harter Stopp.")
    await client.arequest("POST", f"/guests/{name}/action", {"action": "stop"}, timeout=150)


async def _relay(task_id: int, src: AgentClient, dst: AgentClient, name: str, total: int) -> int:
    """Überträgt den Export-Datenstrom der Quelle direkt in den Import des Ziels (durch das Panel)."""
    sent, last_pct, started = 0, -10, time.time()
    timeout = httpx.Timeout(None, connect=20)
    async with httpx.AsyncClient(verify=src.ctx, timeout=timeout) as sc, \
            httpx.AsyncClient(verify=dst.ctx, timeout=timeout) as dc:
        async with sc.stream("GET", f"{src.base}/guests/{name}/export", headers=src.headers) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise AgentError(f"Export: {resp.text[:300]}")

            async def body():
                nonlocal sent, last_pct
                async for chunk in resp.aiter_raw():
                    sent += len(chunk)
                    pct = int(sent * 100 / total) if total else 0
                    if pct >= last_pct + 10:
                        last_pct = pct - pct % 10
                        rate = sent / max(time.time() - started, 1) / (1 << 20)
                        task_log(task_id, f"Übertragen: {sent >> 20} MiB (~{min(pct, 100)} %, {rate:.1f} MiB/s)")
                    yield chunk

            r = await dc.put(f"{dst.base}/guests/{name}/import", headers=dst.headers, content=body())
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                raise AgentError(f"Import: {detail}")
    return sent


async def op_migrate(task_id: int, guest_id: int, target_node_id: int, network: dict, new_ids: list[int],
                     drop_ids: list[int]) -> None:
    """Server auf einen anderen Node umziehen: herunterfahren, übertragen, Netzwerk anpassen, starten, alt löschen."""
    with session_scope() as db:
        g = db.get(Guest, guest_id)
        src, dst = AgentClient.for_node(g.node), AgentClient.for_node(db.get(Node, target_node_id))
        name, src_name, dst_name = g.agent_name, g.node.name, db.get(Node, target_node_id).name
        was_running = g.power == "running"
    _set_guest(guest_id, status="busy")
    imported = False
    try:
        task_log(task_id, f"Umzug von {src_name} nach {dst_name}.")
        if was_running:
            task_log(task_id, "Fahre Server herunter …")
            await _wait_stopped(src, name, task_id)
        info = await src.arequest("GET", f"/guests/{name}/export/info")
        if info.get("kvm_snapshots"):
            task_log(task_id, f"Hinweis: {info['kvm_snapshots']} Snapshot(s) der VM werden nicht mit umgezogen.")
        task_log(task_id, f"Übertrage ca. {info['size'] >> 20} MiB …")
        sent = await _relay(task_id, src, dst, name, info["size"])
        imported = True
        task_log(task_id, f"Übertragung abgeschlossen ({sent >> 20} MiB). Richte Netzwerk auf {dst_name} ein …")
        await agent_job(task_id, dst, "POST", f"/guests/{name}/network", network)
        with session_scope() as db:
            g = db.get(Guest, guest_id)
            g.node_id, g.mac = target_node_id, network["mac"]
            for rid in drop_ids:
                row = db.get(IPAddress, rid)
                if row:
                    db.delete(row)
        from .routers.extras import push_firewall
        try:
            await asyncio.to_thread(push_firewall, guest_id)
        except Exception as exc:  # noqa: BLE001
            task_log(task_id, f"WARNUNG: Firewall auf {dst_name} nicht gesetzt: {exc}")
        if was_running:
            state = (await dst.arequest("POST", f"/guests/{name}/action", {"action": "start"}, timeout=150))["state"]
            _set_guest(guest_id, power=state)
            task_log(task_id, f"Server läuft auf {dst_name}.")
        else:
            _set_guest(guest_id, power="stopped")
        task_log(task_id, f"Entferne alte Kopie auf {src_name} …")
        try:
            await agent_job(task_id, src, "DELETE", f"/guests/{name}")
        except AgentError as exc:
            task_log(task_id, f"WARNUNG: Alte Kopie auf {src_name} konnte nicht gelöscht werden: {exc}")
    except Exception:
        task_log(task_id, "Fehler – mache den Umzug rückgängig …")
        if imported:
            try:
                await agent_job(task_id, dst, "DELETE", f"/guests/{name}")
            except AgentError:
                task_log(task_id, f"WARNUNG: Teilweise importierte Kopie auf {dst_name} bitte prüfen.")
        with session_scope() as db:
            for rid in new_ids:
                row = db.get(IPAddress, rid)
                if row:
                    db.delete(row)
            for rid in drop_ids:
                row = db.get(IPAddress, rid)
                if row:
                    row.guest_id, row.reserved, row.note = guest_id, False, ""
        if was_running:
            try:
                await src.arequest("POST", f"/guests/{name}/action", {"action": "start"}, timeout=150)
                task_log(task_id, f"Server läuft wieder auf {src_name}.")
            except AgentError:
                pass
        raise
    finally:
        _set_guest(guest_id, status="ready")


# ---------------------------------------------------------------- Nodes

def decode_join(code: str) -> dict:
    code = code.strip()
    if code.startswith("POMBOT_JOIN="):
        code = code[len("POMBOT_JOIN="):]
    try:
        data = json.loads(base64.b64decode(code + "=" * (-len(code) % 4)))
        assert data["token"] and data["cert"] and data["host"]
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Ungültiger Join-Code")
    return data


def register_node(name: str, host: str, port: int, token: str, cert: str) -> Node:
    client = AgentClient(host, port, token, cert)
    info = client.request("GET", "/host/info", timeout=20)
    with session_scope() as db:
        if db.scalar(select(Node).where(Node.name == name)):
            raise HTTPException(400, f"Ein Node mit dem Namen {name} existiert bereits")
        node = Node(name=name, host=host, port=port, token=token, cert_pem=cert,
                    status="online", info_json=json.dumps(info))
        db.add(node)
        db.flush()
        return node


def _agent_tarball() -> bytes:
    src = Path(settings.agent_src)
    if not (src / "install-agent.sh").exists():
        raise AgentError(f"Agent-Quellcode nicht gefunden unter {src} (POMBOT_AGENT_SRC)")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in ("pombot_agent", "requirements.txt", "pombot-agent.service", "pombot-network.service",
                     "install-agent.sh", "bridge-setup.sh"):
            tar.add(src / item, arcname=item,
                    filter=lambda ti: None if "__pycache__" in ti.name else ti)
    return buf.getvalue()


def _load_key(text: str, passphrase: str | None):
    import paramiko
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except (paramiko.SSHException, ValueError):
            continue
    raise AgentError("Privater SSH-Schlüssel konnte nicht gelesen werden")


def _ssh_install(task_id: int, p: dict) -> dict:
    import paramiko
    task_log(task_id, f"Verbinde per SSH mit {p['ssh_user']}@{p['host']}:{p['ssh_port']} …")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = _load_key(p["private_key"], p.get("passphrase")) if p.get("private_key") else None
    try:
        client.connect(p["host"], port=p["ssh_port"], username=p["ssh_user"], password=p.get("password") or None,
                       pkey=pkey, timeout=20, allow_agent=False, look_for_keys=False)
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"SSH-Verbindung fehlgeschlagen: {exc}")
    try:
        remote = f"/tmp/pombot-agent-{secrets.token_hex(4)}.tgz"
        task_log(task_id, "Übertrage Agent …")
        sftp = client.open_sftp()
        sftp.putfo(io.BytesIO(_agent_tarball()), remote)
        sftp.close()
        sudo = "" if p["ssh_user"] == "root" else "sudo -S -p '' "
        flags = f"--port {int(p['agent_port'])}"
        if p.get("create_bridge"):
            flags += f" --create-bridge --bridge-name {p['bridge_name']}"
        cmd = (f'set -e; D=$(mktemp -d); tar -xzf {remote} -C "$D"; rm -f {remote}; '
               f'A=$(echo "$SSH_CLIENT" | cut -d" " -f1); '
               f'{sudo}bash "$D/install-agent.sh" --source "$D" --allow "$A" {flags}; rm -rf "$D"')
        task_log(task_id, "Starte Installation (Pakete, libvirt, LXC, Agent) – das dauert einige Minuten …")
        chan = client.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(cmd)
        if sudo and p.get("password"):
            chan.sendall((p["password"] + "\n").encode())
        join, buf, pending = None, b"", []
        while True:
            data = chan.recv(4096)
            if not data:
                break
            buf += data
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.decode(errors="replace").rstrip("\r")
                if line.startswith("POMBOT_JOIN="):
                    join = line
                    pending.append("POMBOT_JOIN=… (empfangen)")
                else:
                    pending.append(line)
            if pending:
                task_log(task_id, *pending)
                pending = []
        code = chan.recv_exit_status()
        if code != 0:
            raise AgentError(f"Installation fehlgeschlagen (Exit-Code {code})")
        if not join:
            raise AgentError("Installer hat keinen Join-Code geliefert")
        return decode_join(join)
    finally:
        client.close()


async def op_install_node(task_id: int, params: dict) -> None:
    data = await asyncio.to_thread(_ssh_install, task_id, params)
    task_log(task_id, f"Zertifikat-Fingerprint: {cert_fingerprint(data['cert'])}")
    task_log(task_id, "Teste Verbindung zum Agent …")
    for attempt in range(10):
        try:
            node = await asyncio.to_thread(register_node, params["name"], params["host"], data["port"],
                                           data["token"], data["cert"])
            task_log(task_id, f"Node {node.name} ist verbunden und einsatzbereit.")
            if params.get("create_bridge"):
                task_log(task_id, f"Hinweis: Bridge {params['bridge_name']} wird im Hintergrund angelegt. "
                                  "Die Bridge-Liste aktualisiert sich innerhalb weniger Minuten.")
            return
        except AgentError as exc:
            task_log(task_id, f"Agent noch nicht erreichbar ({exc}) – neuer Versuch …")
            await asyncio.sleep(3 + attempt)
    raise AgentError("Agent ist nach der Installation nicht erreichbar. Firewall/Port prüfen.")
