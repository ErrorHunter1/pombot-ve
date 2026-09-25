"""Nodes (physische Hosts): hinzufügen per SSH oder Join-Code, Status, Images, Shell."""
import json
import re

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ops
from ..agent_client import AgentClient, AgentError, cert_fingerprint
from ..db import SessionLocal, get_db
from ..models import Guest, Node, User
from ..security import audit, client_ip, current_user, require_admin
from ..tasks import NODE_HISTORY, NODE_STATS, agent_job, create_task, run_task, submit
from .guests import proxy_websocket, same_origin, ws_user

router = APIRouter(prefix="/api/nodes", tags=["nodes"])
NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"


def node_dict(n: Node, admin: bool) -> dict:
    info = n.info
    stats = NODE_STATS.get(n.id)
    data = {
        "id": n.id, "name": n.name, "status": n.status, "enabled": n.enabled,
        "guests": len(n.guests),
        "committed_memory_mb": sum(g.memory_mb for g in n.guests),
        "committed_cores": sum(g.cores for g in n.guests),
        "info": {k: info.get(k) for k in ("os", "cpu_cores", "memory_total", "disk_total", "kvm", "bridges",
                                          "libvirt", "lxc", "cpu_model", "kernel", "agent_version", "hostname", "virtualization",
                                          "main_ipv4")},
        "stats": stats,
        "last_seen": n.last_seen.isoformat() + "Z" if n.last_seen else None,
    }
    if admin:
        data.update({"host": n.host, "port": n.port, "fingerprint": cert_fingerprint(n.cert_pem)})
    return data


@router.get("")
def list_nodes(user: User = Depends(current_user), db: Session = Depends(get_db)):
    nodes = db.scalars(select(Node).order_by(Node.name))
    return [node_dict(n, user.is_admin) for n in nodes if user.is_admin or n.enabled]


@router.get("/{node_id}")
def get_node(node_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node nicht gefunden")
    data = node_dict(node, True)
    data["history"] = list(NODE_HISTORY.get(node.id, []))
    return data


class SSHBody(BaseModel):
    name: str = Field(pattern=NAME)
    host: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None
    agent_port: int = Field(default=8007, ge=1, le=65535)
    create_bridge: bool = False
    bridge_name: str = Field(default="vmbr0", pattern=r"^[A-Za-z0-9_.-]{1,15}$")


@router.post("/ssh")
def add_node_ssh(body: SSHBody, request: Request, user: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    if not body.password and not body.private_key:
        raise HTTPException(400, "Bitte Passwort oder privaten SSH-Schlüssel angeben")
    if db.scalar(select(Node).where(Node.name == body.name)):
        raise HTTPException(400, "Name bereits vergeben")
    task = create_task(db, user.id, "node-install", f"{body.name} ({body.host})")
    audit(db, user, "node-install", f"{body.name} {body.ssh_user}@{body.host}", client_ip(request))
    db.commit()
    # Zugangsdaten werden nur für die Installation im Speicher gehalten, nicht gespeichert.
    submit(run_task(task.id, ops.op_install_node(task.id, body.model_dump())))
    return {"task_id": task.id}


class JoinBody(BaseModel):
    name: str | None = Field(default=None, pattern=NAME)
    join_code: str
    host: str | None = None  # optional abweichende Adresse


@router.post("/join")
def add_node_join(body: JoinBody, request: Request, user: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    data = ops.decode_join(body.join_code)
    name = body.name or data.get("name") or data["host"]
    try:
        node = ops.register_node(name, body.host or data["host"], int(data.get("port", 8007)),
                                 data["token"], data["cert"])
    except AgentError as exc:
        raise HTTPException(502, f"Agent nicht erreichbar: {exc}")
    audit(db, user, "node-join", f"{node.name} {node.host}", client_ip(request))
    db.commit()
    return {"id": node.id}


class NodePatch(BaseModel):
    name: str | None = Field(default=None, pattern=NAME)
    host: str | None = None
    enabled: bool | None = None


@router.patch("/{node_id}")
def patch_node(node_id: int, body: NodePatch, request: Request, user: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node nicht gefunden")
    for field in ("name", "host", "enabled"):
        value = getattr(body, field)
        if value is not None:
            setattr(node, field, value)
    audit(db, user, "node-update", f"{node.name}", client_ip(request))
    db.commit()
    return node_dict(node, True)


@router.post("/{node_id}/refresh")
def refresh_node(node_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node nicht gefunden")
    try:
        info = AgentClient.for_node(node).request("GET", "/host/info", timeout=20)
    except AgentError as exc:
        node.status = "offline"
        db.commit()
        raise HTTPException(502, str(exc))
    node.info_json, node.status = json.dumps(info), "online"
    db.commit()
    return node_dict(node, True)


@router.delete("/{node_id}")
def delete_node(node_id: int, request: Request, user: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node nicht gefunden")
    if db.scalar(select(Guest.id).where(Guest.node_id == node.id)):
        raise HTTPException(400, "Auf diesem Node befinden sich noch Server – bitte zuerst löschen")
    audit(db, user, "node-delete", node.name, client_ip(request))
    db.delete(node)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Images auf dem Node

def _node_client(db: Session, node_id: int) -> AgentClient:
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Node nicht gefunden")
    return AgentClient.for_node(node)


@router.get("/{node_id}/images")
def node_images(node_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        return _node_client(db, node_id).request("GET", "/images")
    except AgentError as exc:
        raise HTTPException(502, str(exc))


@router.delete("/{node_id}/images/{image_id}")
def node_image_delete(node_id: int, image_id: str, user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    try:
        return _node_client(db, node_id).request("DELETE", f"/images/{image_id}")
    except AgentError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{node_id}/network")
def node_network(node_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Automatisch erkannte IPv4-Adressen und Gateways des Nodes, inkl. Hinweis, ob schon in einem Pool."""
    from .. import ipam
    from ..models import IPPool
    try:
        data = _node_client(db, node_id).request("GET", "/host/network", timeout=20)
    except AgentError as exc:
        raise HTTPException(502, f"Erkennung fehlgeschlagen (Agent aktuell?): {exc}")
    pools = list(db.scalars(select(IPPool)))
    for a in data.get("addresses", []):
        a["pool"] = next((p.name for p in pools if ipam.version_of(p) == 4 and ipam.contains(p, a["address"])), None)
    return data


# ---------------------------------------------------------------- ISO-Bibliothek

UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")


def _iso_client(db: Session, node_id: int, user: User) -> AgentClient:
    node = db.get(Node, node_id)
    if not node or (not user.is_admin and not node.enabled):
        raise HTTPException(404, "Node nicht gefunden")
    return AgentClient.for_node(node)


def _agent_call(fn):
    try:
        return fn()
    except AgentError as exc:
        raise HTTPException(400, str(exc))


@router.get("/{node_id}/isos")
def node_isos(node_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    client = _iso_client(db, node_id, user)
    return _agent_call(lambda: client.request("GET", "/isos"))


@router.delete("/{node_id}/isos/{name}")
def node_iso_delete(node_id: int, name: str, request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    in_use = [g for g in db.scalars(select(Guest).where(Guest.node_id == node_id, Guest.iso_file == name))]
    if in_use:
        raise HTTPException(400, f"Die ISO wird noch von {', '.join(g.name for g in in_use)} verwendet")
    client = _iso_client(db, node_id, user)
    _agent_call(lambda: client.request("DELETE", f"/isos/{name}"))
    audit(db, user, "iso-delete", name, client_ip(request))
    db.commit()
    return {"ok": True}


@router.post("/{node_id}/isos/uploads")
def node_iso_upload_start(node_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    client = _iso_client(db, node_id, user)
    return _agent_call(lambda: client.request("POST", "/isos/uploads"))


@router.get("/{node_id}/isos/uploads/{upload_id}")
def node_iso_upload_status(node_id: int, upload_id: str, user: User = Depends(require_admin),
                           db: Session = Depends(get_db)):
    if not UPLOAD_ID.match(upload_id):
        raise HTTPException(400, "Ungültige Upload-ID")
    client = _iso_client(db, node_id, user)
    return _agent_call(lambda: client.request("GET", f"/isos/uploads/{upload_id}"))


@router.put("/{node_id}/isos/uploads/{upload_id}")
async def node_iso_upload_chunk(node_id: int, upload_id: str, request: Request, offset: int = 0,
                                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Reicht ein Stück der Datei als Datenstrom an den Agent weiter (ohne Zwischenspeicher)."""
    if not UPLOAD_ID.match(upload_id):
        raise HTTPException(400, "Ungültige Upload-ID")
    client = _iso_client(db, node_id, user)
    try:
        async with httpx.AsyncClient(verify=client.ctx, timeout=httpx.Timeout(600, connect=15)) as http:
            resp = await http.put(f"{client.base}/isos/uploads/{upload_id}", params={"offset": offset},
                                  headers=client.headers, content=request.stream())
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Node nicht erreichbar: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(400, resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text)
    return resp.json()


class IsoFinishBody(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    size: int | None = None


@router.post("/{node_id}/isos/uploads/{upload_id}/finish")
def node_iso_upload_finish(node_id: int, upload_id: str, body: IsoFinishBody, request: Request,
                           user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if not UPLOAD_ID.match(upload_id):
        raise HTTPException(400, "Ungültige Upload-ID")
    client = _iso_client(db, node_id, user)
    result = _agent_call(lambda: client.request("POST", f"/isos/uploads/{upload_id}/finish", body.model_dump(),
                                                timeout=120))
    audit(db, user, "iso-upload", f"{result['file']} ({result['size'] >> 20} MiB)", client_ip(request))
    db.commit()
    return result


@router.delete("/{node_id}/isos/uploads/{upload_id}")
def node_iso_upload_abort(node_id: int, upload_id: str, user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    if UPLOAD_ID.match(upload_id):
        client = _iso_client(db, node_id, user)
        _agent_call(lambda: client.request("DELETE", f"/isos/uploads/{upload_id}"))
    return {"ok": True}


class IsoFetchBody(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=1000)
    name: str = Field(min_length=1, max_length=160)


@router.post("/{node_id}/isos/fetch")
def node_iso_fetch(node_id: int, body: IsoFetchBody, request: Request, user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    client = _iso_client(db, node_id, user)
    task = create_task(db, user.id, "iso-fetch", f"{body.name} ({db.get(Node, node_id).name})", node_id)
    audit(db, user, "iso-fetch", body.url, client_ip(request))
    db.commit()
    submit(run_task(task.id, agent_job(task.id, client, "POST", "/isos/fetch", body.model_dump())))
    return {"task_id": task.id}


@router.get("/{node_id}/backups")
def node_backups(node_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        return _node_client(db, node_id).request("GET", "/backups")
    except AgentError as exc:
        raise HTTPException(502, str(exc))


# ---------------------------------------------------------------- Host-Shell

@router.websocket("/{node_id}/shell")
async def node_shell(ws: WebSocket, node_id: int):
    if not same_origin(ws):
        await ws.close(code=4403)
        return
    with SessionLocal() as db:
        user = ws_user(ws, db)
        node = db.get(Node, node_id)
        if not user or not user.is_admin or not node:
            await ws.close(code=4403)
            return
        client = AgentClient.for_node(node)
        audit(db, user, "node-shell", node.name, ws.client.host if ws.client else "")
        db.commit()
    await proxy_websocket(ws, client, "/host/shell")
