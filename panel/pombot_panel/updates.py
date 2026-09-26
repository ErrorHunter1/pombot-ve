"""Updates: prüft regelmäßig, ob auf GitHub eine neue Version erschienen ist, und aktualisiert auf Knopfdruck
alles im Hintergrund.

Ablauf beim Klick auf „Update starten“:
1. Das Panel legt die Zielversion in <data_dir>/update/request ab. pombot-update.path (systemd) startet
   daraufhin pombot-update.service als root: .deb-Pakete laden und installieren – das Panel und, falls auf
   demselben Server installiert, den Agent. Das Panel startet dabei neu.
2. Das neue Panel sieht beim Start den offenen Auftrag und bittet danach jeden Node, sich selbst auf
   dieselbe Version zu aktualisieren (Agent-Endpunkt /system/update, ab Agent 0.8.0). Nodes, die gerade
   offline sind, folgen, sobald sie wieder erreichbar sind.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .agent_client import AgentClient, AgentError
from .config import settings
from .db import get_db, session_scope
from .models import Node, Setting, User
from .perms import require
from .security import audit, client_ip

log = logging.getLogger("pombot.updates")
router = APIRouter(prefix="/api/admin/update", tags=["updates"])

TAG_RE = re.compile(r"^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$")
REQUEST = Path(settings.data_dir) / "update" / "request"
HELPER = Path("/etc/systemd/system/pombot-update.path")
PANEL_STATUS = Path("/var/log/pombot-update.json")
PANEL_LOG = Path("/var/log/pombot-update.log")
NODE_RETRY = 900  # Sekunden bis zum erneuten Versuch bei einem Node

_node_state: dict[int, dict] = {}  # node_id -> {"state", "message", "time"}
_jobs: set = set()


def parse(version: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", (version or "").split("-")[0])
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def newer(a: str, b: str) -> bool:
    """Ist Version a neuer als b?"""
    return parse(a) > parse(b)


def _get(db: Session, key: str, default=None):
    row = db.get(Setting, key)
    if not row:
        return default
    try:
        return json.loads(row.value)
    except ValueError:
        return default


def _set(db: Session, key: str, value) -> None:
    row = db.get(Setting, key) or Setting(key=key)
    row.value = json.dumps(value)
    db.add(row)


# ---------------------------------------------------------------- Prüfen

async def check_now() -> dict:
    """Fragt die neueste Version auf GitHub ab und merkt sie sich."""
    url = f"https://api.github.com/repos/{settings.update_repo}/releases/latest"
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": f"pombot-ve/{settings.version}",
                                                      "Accept": "application/vnd.github+json"}) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise AgentError(f"GitHub antwortet mit HTTP {resp.status_code}")
    data = resp.json()
    tag = str(data.get("tag_name", ""))
    if not TAG_RE.match(tag):
        raise AgentError(f"Unerwartete Versionsangabe: {tag!r}")
    latest = {"tag": tag, "version": tag[1:], "url": data.get("html_url", ""),
              "published_at": data.get("published_at", ""), "notes": (data.get("body") or "")[:4000]}
    with session_scope() as db:
        _set(db, "update.latest", latest)
        _set(db, "update.checked_at", time.time())
        _set(db, "update.error", None)
    if newer(latest["version"], settings.version):
        log.info("Update verfügbar: %s (installiert: %s)", tag, settings.version)
    return latest


async def update_loop() -> None:
    """Prüft alle `update_check_hours` Stunden (0 = nie) und kümmert sich um offene Node-Updates."""
    await asyncio.sleep(30)
    while True:
        try:
            hours = int(getattr(settings, "update_check_hours", 6) or 0)
            with session_scope() as db:
                last = float(_get(db, "update.checked_at", 0) or 0)
            if hours > 0 and time.time() - last >= hours * 3600:
                try:
                    await check_now()
                except Exception as exc:  # noqa: BLE001
                    log.warning("Update-Prüfung fehlgeschlagen: %s", exc)
                    with session_scope() as db:
                        _set(db, "update.checked_at", time.time())
                        _set(db, "update.error", str(exc))
            await update_nodes()
        except Exception:  # noqa: BLE001
            log.exception("Fehler in der Update-Schleife")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- Nodes

def _agent_version(node: Node) -> str:
    return str(node.info.get("agent_version") or "")


async def update_nodes(force_node: int | None = None) -> None:
    """Offener Auftrag: alle Nodes auf die Version des Panels bringen (sobald das Panel sie hat)."""
    with session_scope() as db:
        tag = _get(db, "update.nodes_tag")
        if not tag and force_node is None:
            return
        tag = tag or f"v{settings.version}"
        if parse(settings.version) < parse(tag):
            return  # Panel ist noch nicht aktualisiert – das neue Panel übernimmt nach dem Neustart
        tag = f"v{settings.version}"
        only = _get(db, "update.nodes_ids") if force_node is None else None
        nodes = [(n.id, n.name, n.status, _agent_version(n), AgentClient.for_node(n))
                 for n in db.scalars(select(Node))
                 if force_node in (None, n.id) and (only is None or n.id in only)]
    pending = False
    for node_id, name, status, version, client in nodes:
        state = _node_state.get(node_id, {})
        if version and not newer(tag[1:], version):
            _node_state.pop(node_id, None)
            continue
        pending = True
        if status != "online":
            continue
        try:
            remote = await client.arequest("GET", "/system/update", timeout=20)
        except AgentError as exc:
            if "Not Found" in str(exc) or "404" in str(exc):
                _node_state[node_id] = {"state": "manual", "time": time.time(),
                                        "message": "Agent zu alt für Updates aus dem Panel – einmalig auf dem Node aktualisieren"}
            continue
        if remote.get("version") and not newer(tag[1:], remote["version"]):
            _store_version(node_id, remote["version"])
            _node_state[node_id] = {"state": "ok", "time": time.time(), "message": f"Aktualisiert auf v{remote['version']}"}
            continue
        if remote.get("state") == "running" and time.time() - float(remote.get("time", 0)) < 1800:
            _node_state[node_id] = {"state": "running", "time": time.time(), "message": remote.get("message", "")}
            continue
        if state.get("state") in ("requested", "running", "error") and time.time() - state.get("time", 0) < NODE_RETRY \
                and force_node is None:
            if remote.get("state") == "error":
                _node_state[node_id] = {**state, "state": "error", "message": remote.get("message", "Fehler")}
            continue
        try:
            await client.arequest("POST", "/system/update", {"tag": tag}, timeout=30)
            _node_state[node_id] = {"state": "requested", "time": time.time(), "message": f"Update auf {tag} gestartet"}
            log.info("Node %s: Update auf %s gestartet", name, tag)
        except AgentError as exc:
            _node_state[node_id] = {"state": "error", "time": time.time(), "message": str(exc)}
    if not pending and force_node is None:
        with session_scope() as db:
            _set(db, "update.nodes_tag", None)
            _set(db, "update.nodes_ids", None)


def _store_version(node_id: int, version: str) -> None:
    with session_scope() as db:
        node = db.get(Node, node_id)
        if node:
            info = node.info
            info["agent_version"] = version
            node.info_json = json.dumps(info)


# ---------------------------------------------------------------- API

def _panel_status() -> dict:
    try:
        data = json.loads(PANEL_STATUS.read_text())
    except (OSError, ValueError):
        data = {"state": "idle"}
    try:
        data["log"] = PANEL_LOG.read_text(errors="replace").splitlines()[-60:]
    except OSError:
        data["log"] = []
    if REQUEST.exists():
        data.update(state="queued", message="Auftrag wartet auf den Update-Dienst …")
    return data


@router.get("")
def update_info(user: User = Depends(require("updates.manage")), db: Session = Depends(get_db)):
    latest = _get(db, "update.latest")
    checked = _get(db, "update.checked_at")
    nodes = []
    for n in db.scalars(select(Node).order_by(Node.name)):
        v = _agent_version(n)
        nodes.append({"id": n.id, "name": n.name, "status": n.status, "agent_version": v or None,
                      "local": n.host in LOCAL_HOSTS,
                      "outdated": bool(v) and newer(settings.version, v), **_node_state.get(n.id, {})})
    return {
        "current": settings.version, "repo": settings.update_repo,
        "check_hours": int(getattr(settings, "update_check_hours", 6) or 0),
        "latest": latest, "available": bool(latest and newer(latest["version"], settings.version)),
        "checked_at": datetime.fromtimestamp(checked, timezone.utc).isoformat() if checked else None,
        "error": _get(db, "update.error"), "helper": HELPER.exists(),
        "panel": _panel_status(), "nodes": nodes, "nodes_tag": _get(db, "update.nodes_tag"),
        "nodes_ids": _get(db, "update.nodes_ids"),
    }


@router.post("/check")
async def update_check(user: User = Depends(require("updates.manage"))):
    try:
        await check_now()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Prüfung fehlgeschlagen: {exc}")
    with session_scope() as db:
        return update_info(user, db)


class StartBody(BaseModel):
    tag: str | None = Field(default=None, pattern=r"^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$")
    scope: str = Field(default="all", pattern="^(all|panel|nodes)$")  # beides | nur Panel | nur Nodes
    node_ids: list[int] | None = None  # None = alle Nodes


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@router.post("/start")
async def update_start(request: Request, body: StartBody | None = None, user: User = Depends(require("updates.manage"))):
    body = body or StartBody()
    with session_scope() as db:
        latest = _get(db, "update.latest")
        tag = body.tag or (latest or {}).get("tag")
        nodes = list(db.scalars(select(Node)))
        ids = None if body.node_ids is None else sorted({i for i in body.node_ids if any(n.id == i for n in nodes)})
        do_panel = body.scope in ("all", "panel")
        do_nodes = body.scope in ("all", "nodes")
        if do_nodes and ids == []:
            if body.scope == "nodes":
                raise HTTPException(400, "Bitte mindestens einen Node auswählen")
            do_nodes = False
        if do_panel:
            if not tag:
                raise HTTPException(400, "Noch keine Version bekannt – bitte zuerst „Jetzt prüfen“")
            if not newer(tag[1:], settings.version):
                if body.scope == "panel":
                    raise HTTPException(400, f"Das Panel ist bereits auf v{settings.version}")
                do_panel = False
        if do_panel and not HELPER.exists():
            raise HTTPException(400, "Der Update-Dienst ist auf diesem Server nicht eingerichtet (ältere Installation). "
                                     "Bitte einmalig per Kommandozeile aktualisieren – danach geht es per Knopfdruck.")
        if do_panel and _panel_status().get("state") in ("queued", "running"):
            raise HTTPException(409, "Es läuft bereits ein Update")
        # Nodes bekommen immer die Version des Panels (nach dessen Update die neue)
        node_tag = tag if do_panel else f"v{settings.version}"
        if do_nodes:
            _set(db, "update.nodes_tag", node_tag)
            _set(db, "update.nodes_ids", ids)
        elif body.scope == "panel":  # ausdrücklich nur das Panel: offene Node-Aufträge verwerfen
            _set(db, "update.nodes_tag", None)
            _set(db, "update.nodes_ids", None)
        what = {"all": "Panel + Nodes", "panel": "nur Panel", "nodes": "nur Nodes"}[body.scope]
        audit(db, user, "update-start", f"{what}: {settings.version} -> {tag if do_panel else node_tag}"
              + (f", Nodes {ids}" if do_nodes and ids is not None else ""), client_ip(request))
        # Agent auf dem Panel-Server nur mit aktualisieren, wenn dieser Node mit ausgewählt ist
        local_ids = [n.id for n in nodes if n.host in LOCAL_HOSTS]
        local_agent = do_nodes and (ids is None or any(i in ids for i in local_ids))
    if not do_panel and not do_nodes:
        raise HTTPException(400, "Nichts zu tun – Panel und ausgewählte Nodes sind aktuell")
    if do_panel:
        REQUEST.parent.mkdir(parents=True, exist_ok=True)
        tmp = REQUEST.with_suffix(".tmp")
        tmp.write_text(f"{tag} {'all' if local_agent else 'panel'}")
        tmp.replace(REQUEST)  # atomar, damit der Update-Dienst nie eine halbe Datei liest
        msg = f"Update auf {tag} gestartet – das Panel startet gleich neu."
        if do_nodes:
            msg += " Danach folgen die Nodes."
        return {"ok": True, "panel": True, "message": msg}
    _node_state.clear()
    job = asyncio.create_task(update_nodes())
    _jobs.add(job)
    job.add_done_callback(_jobs.discard)
    return {"ok": True, "panel": False, "message": f"Nodes werden auf {node_tag} aktualisiert."}


@router.post("/nodes/{node_id}")
async def update_node(node_id: int, request: Request, user: User = Depends(require("updates.manage"))):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            raise HTTPException(404, "Node nicht gefunden")
        audit(db, user, "update-node", f"{node.name} -> v{settings.version}", client_ip(request))
    _node_state.pop(node_id, None)
    await update_nodes(force_node=node_id)
    return _node_state.get(node_id, {"state": "ok", "message": "Node ist aktuell"})
