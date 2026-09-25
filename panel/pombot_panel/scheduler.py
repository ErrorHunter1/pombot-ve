"""Führt zeitgesteuerte Backups aus und löscht alte automatische Backups (Aufbewahrung)."""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from .agent_client import AgentClient
from .db import session_scope
from .models import BackupSchedule, Guest

log = logging.getLogger("pombot.scheduler")
# Pro Node läuft immer nur ein automatisches Backup gleichzeitig, um die Festplatten zu schonen.
_NODE_LOCKS: dict[int, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(1))


def last_slot(s: BackupSchedule, ref: datetime) -> datetime:
    """Letzter planmäßiger Termin, der nicht nach `ref` liegt (Serverzeit)."""
    slot = ref.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
    if s.frequency == "weekly":
        slot -= timedelta(days=(ref.weekday() - s.weekday) % 7)
        if slot > ref:
            slot -= timedelta(days=7)
    elif slot > ref:
        slot -= timedelta(days=1)
    return slot


def next_run(s: BackupSchedule, ref: datetime | None = None) -> datetime:
    ref = ref or datetime.now()
    step = timedelta(days=7 if s.frequency == "weekly" else 1)
    return last_slot(s, ref) + step


def _set_status(guest_id: int, status: str) -> None:
    with session_scope() as db:
        s = db.get(BackupSchedule, guest_id)
        if s:
            s.last_status = status


async def _auto_backup(task_id: int, guest_id: int, node_id: int, keep: int) -> None:
    from . import ops
    from .tasks import task_log

    async with _NODE_LOCKS[node_id]:
        await ops.op_job(task_id, guest_id, "POST", "/guests/{name}/backups?auto=true")
        with session_scope() as db:
            g = db.get(Guest, guest_id)
            client, name = AgentClient.for_node(g.node), g.agent_name
        backups = await client.arequest("GET", f"/backups?name={name}")
        autos = sorted((b for b in backups if b.get("auto")), key=lambda b: b["created"], reverse=True)
        for old in autos[keep:]:
            await client.arequest("DELETE", f"/backups/{old['file']}")
            task_log(task_id, f"Altes automatisches Backup gelöscht: {old['file']}")
        task_log(task_id, f"Aufbewahrt: {min(len(autos), keep)} von maximal {keep} automatischen Backups.")
    _set_status(guest_id, "ok")


def _collect_due() -> list[tuple[int, int, int, int]]:
    from .tasks import create_task

    now = datetime.now().replace(microsecond=0)
    due = []
    with session_scope() as db:
        for s in db.scalars(select(BackupSchedule).where(BackupSchedule.enabled.is_(True))):
            g = db.get(Guest, s.guest_id)
            if not g or g.status != "ready":
                continue  # beschäftigt/fehlerhaft: beim nächsten Durchlauf erneut prüfen
            if s.last_run and s.last_run >= last_slot(s, now):
                continue
            s.last_run, s.last_status = now, "running"
            task = create_task(db, g.owner_id, "backup-auto", f"{g.name} (#{g.vmid})", g.node_id, g.id)
            due.append((task.id, g.id, g.node_id, s.keep))
    return due


async def scheduler_loop() -> None:
    from .tasks import _RUNNING, run_task

    await asyncio.sleep(15)
    while True:
        try:
            for task_id, guest_id, node_id, keep in _collect_due():
                log.info("Starte automatisches Backup für Server %s", guest_id)
                t = asyncio.create_task(run_task(task_id, _auto_backup(task_id, guest_id, node_id, keep),
                                                 on_error=lambda _msg, gid=guest_id: _set_status(gid, "error")))
                _RUNNING.add(t)
                t.add_done_callback(_RUNNING.discard)
        except Exception:  # noqa: BLE001
            log.exception("Scheduler-Fehler")
        await asyncio.sleep(30)
