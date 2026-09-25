"""Führt zeitgesteuerte Backups aus und löscht alte automatische Backups (Aufbewahrung)."""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import session_scope
from .models import BackupSchedule, BackupTarget, Guest

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


async def _auto_backup(task_id: int, guest_id: int, node_id: int, keep: int, target_id: int | None,
                       keep_local: bool) -> None:
    from . import backup_targets, ops

    with session_scope() as db:
        g = db.get(Guest, guest_id)
        t = db.get(BackupTarget, target_id) if target_id else None
        target, sub = (backup_targets.payload(t) if t else None), backup_targets.subdir(g)
    async with _NODE_LOCKS[node_id]:
        await ops.op_backup(task_id, guest_id, target, sub, keep_local=keep_local, auto=True, keep=keep)
    _set_status(guest_id, "ok")


def _collect_due() -> list[tuple]:
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
            due.append((task.id, g.id, g.node_id, s.keep, s.target_id, s.keep_local))
    return due


async def scheduler_loop() -> None:
    from .tasks import _RUNNING, run_task

    await asyncio.sleep(15)
    last_tls_check = 0.0
    while True:
        if asyncio.get_running_loop().time() - last_tls_check > 12 * 3600:
            last_tls_check = asyncio.get_running_loop().time()
            from .routers.admin import renew_if_needed
            try:
                await asyncio.to_thread(renew_if_needed)
            except Exception:  # noqa: BLE001
                log.exception("Zertifikatsprüfung fehlgeschlagen")
        try:
            for task_id, guest_id, node_id, keep, target_id, keep_local in _collect_due():
                log.info("Starte automatisches Backup für Server %s", guest_id)
                t = asyncio.create_task(run_task(task_id, _auto_backup(task_id, guest_id, node_id, keep, target_id, keep_local),
                                                 on_error=lambda _msg, gid=guest_id: _set_status(gid, "error")))
                _RUNNING.add(t)
                t.add_done_callback(_RUNNING.discard)
        except Exception:  # noqa: BLE001
            log.exception("Scheduler-Fehler")
        await asyncio.sleep(30)
