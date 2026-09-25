"""Hintergrund-Aufgaben (mit Log) und Status-Abfrage der Nodes."""
import asyncio
import json
import logging
from collections import defaultdict, deque

from sqlalchemy import select

from .agent_client import AgentClient, AgentError
from .config import settings
from .db import session_scope
from .models import Guest, Node, Task, now

log = logging.getLogger("pombot.tasks")

LOOP: asyncio.AbstractEventLoop | None = None
_RUNNING: set = set()

# Live-Werte der Nodes (nicht in der DB, da sie sich ständig ändern)
NODE_STATS: dict[int, dict] = {}
NODE_HISTORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=180))
GUEST_BUSY_STATES = {"creating", "deleting", "busy"}


# ---------------------------------------------------------------- Task-Log

def create_task(db, user_id: int | None, action: str, target: str = "",
                node_id: int | None = None, guest_id: int | None = None) -> Task:
    task = Task(user_id=user_id, action=action, target=target, node_id=node_id, guest_id=guest_id)
    db.add(task)
    db.flush()
    return task


def task_log(task_id: int, *lines: str) -> None:
    if not lines:
        return
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task:
            task.log = (task.log or "") + "".join(f"{line}\n" for line in lines)


def task_finish(task_id: int, ok: bool, message: str | None = None) -> None:
    with session_scope() as db:
        task = db.get(Task, task_id)
        if task:
            if message:
                task.log = (task.log or "") + message + "\n"
            task.status = "ok" if ok else "error"
            task.finished_at = now()


def submit(coro) -> None:
    """Startet eine Coroutine im Haupt-Eventloop – auch aus synchronen Endpunkten (Threadpool)."""
    if LOOP is None:
        raise RuntimeError("Eventloop nicht initialisiert")
    fut = asyncio.run_coroutine_threadsafe(coro, LOOP)
    _RUNNING.add(fut)
    fut.add_done_callback(_RUNNING.discard)


async def run_task(task_id: int, coro, on_error=None):
    """Führt `coro` aus und schließt den Task entsprechend ab."""
    try:
        await coro
        task_finish(task_id, True)
    except Exception as exc:  # noqa: BLE001
        log.exception("Task %s fehlgeschlagen", task_id)
        task_finish(task_id, False, f"FEHLER: {exc}")
        if on_error:
            try:
                on_error(str(exc))
            except Exception:  # noqa: BLE001
                log.exception("on_error fehlgeschlagen")


async def agent_job(task_id: int, client: AgentClient, method: str, path: str, payload=None,
                    max_seconds: int = 6 * 3600):
    """Startet einen Job auf dem Agent und spiegelt dessen Log in den Task."""
    started = await client.arequest(method, path, json=payload, timeout=60)
    job_id = started["job"]
    since, waited, errors = 0, 0.0, 0
    while waited < max_seconds:
        await asyncio.sleep(1.5)
        waited += 1.5
        try:
            job = await client.arequest("GET", f"/jobs/{job_id}?since={since}", timeout=30)
            errors = 0
        except AgentError:
            errors += 1
            if errors > 20:
                raise
            continue
        if job["lines"]:
            task_log(task_id, *job["lines"])
        since = job["next"]
        if job["status"] == "ok":
            return job.get("result") or {}
        if job["status"] == "error":
            raise AgentError(job.get("error") or "Unbekannter Fehler")
    raise AgentError("Zeitüberschreitung beim Warten auf den Agent")


def mark_stale_tasks() -> None:
    with session_scope() as db:
        for task in db.scalars(select(Task).where(Task.status == "running")):
            task.status = "error"
            task.log = (task.log or "") + "Abgebrochen: Panel wurde neu gestartet.\n"
            task.finished_at = now()
        for guest in db.scalars(select(Guest).where(Guest.status.in_(GUEST_BUSY_STATES))):
            guest.status = "error"
            guest.error = "Vorgang durch Neustart des Panels unterbrochen"


# ---------------------------------------------------------------- Poller

async def _poll_node(node_id: int, client: AgentClient, refresh_info: bool) -> None:
    try:
        stats = await client.arequest("GET", "/host/stats", timeout=10)
        guests = await client.arequest("GET", "/guests", timeout=20)
        info = await client.arequest("GET", "/host/info", timeout=20) if refresh_info else None
    except AgentError as exc:
        NODE_STATS.pop(node_id, None)
        with session_scope() as db:
            node = db.get(Node, node_id)
            if node and node.status != "offline":
                log.warning("Node %s offline: %s", node.name, exc)
                node.status = "offline"
            for guest in db.scalars(select(Guest).where(Guest.node_id == node_id)):
                guest.power = "unknown"
        return
    NODE_STATS[node_id] = stats
    mem = stats["memory_used"] / stats["memory_total"] * 100 if stats.get("memory_total") else 0
    NODE_HISTORY[node_id].append({"t": int(stats["time"]), "cpu": stats["cpu"], "mem": round(mem, 1)})
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            return
        node.status = "online"
        node.last_seen = now()
        if info:
            node.info_json = json.dumps(info)
        for guest in db.scalars(select(Guest).where(Guest.node_id == node_id)):
            entry = guests.get(guest.agent_name)
            if guest.status in GUEST_BUSY_STATES:
                continue
            guest.power = entry["state"] if entry else "missing"


async def poll_loop() -> None:
    counter = 0
    while True:
        try:
            with session_scope() as db:
                nodes = [(n.id, AgentClient.for_node(n)) for n in db.scalars(select(Node))]
            refresh = counter % 30 == 0
            await asyncio.gather(*(_poll_node(nid, client, refresh) for nid, client in nodes))
        except Exception:  # noqa: BLE001
            log.exception("Poller-Fehler")
        counter += 1
        await asyncio.sleep(settings.poll_interval)
