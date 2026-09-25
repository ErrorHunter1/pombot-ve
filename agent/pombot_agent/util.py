"""Hilfsfunktionen: Befehle ausführen, Hintergrund-Jobs, Sperren."""
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager, nullcontext

NAME_RE = re.compile(r"^pv\d{1,9}$")
SNAP_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class CmdError(Exception):
    pass


class Busy(Exception):
    pass


def run(cmd, check=True, timeout=900, input_text=None, job=None, env=None, log=True):
    """Führt einen Befehl aus und gibt stdout zurück. Wirft CmdError bei Fehler."""
    cmd = [str(c) for c in cmd]
    if job is not None and log:
        job.write("$ " + " ".join(shlex.quote(c) for c in cmd))
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=input_text, env=full_env)
    except FileNotFoundError:
        raise CmdError(f"Programm nicht gefunden: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise CmdError(f"Zeitüberschreitung bei: {cmd[0]}")
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()
        raise CmdError(f"{cmd[0]} fehlgeschlagen (Code {p.returncode}): {msg[-2000:]}")
    return p.stdout


def ok(cmd, timeout=60) -> bool:
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise CmdError("Ungültiger Gastname")
    return name


def check_snap(snap: str) -> str:
    if not SNAP_RE.match(snap or ""):
        raise CmdError("Ungültiger Snapshot-Name (erlaubt: A-Z, a-z, 0-9, _ und -)")
    return snap


# ---------------------------------------------------------------- Jobs

class Job:
    def __init__(self, kind: str, target: str | None):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.target = target
        self.status = "running"
        self.lines: list[str] = []
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None

    def write(self, msg) -> None:
        stamp = time.strftime("%H:%M:%S")
        for line in (str(msg).splitlines() or [""]):
            self.lines.append(f"[{stamp}] {line}")

    def as_dict(self, since: int = 0) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "lines": self.lines[since:],
            "next": len(self.lines),
            "result": self.result,
            "error": self.error,
        }


JOBS: dict[str, Job] = {}
_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)
_LOCKS_GUARD = threading.Lock()


def guest_lock(name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS[name]


@contextmanager
def try_lock(name: str, timeout: float = 3):
    lock = guest_lock(name)
    if not lock.acquire(timeout=timeout):
        raise Busy(f"{name} ist gerade mit einer anderen Aufgabe beschäftigt")
    try:
        yield
    finally:
        lock.release()


def _prune_jobs() -> None:
    cutoff = time.time() - 3600
    for jid in [j.id for j in JOBS.values() if j.finished and j.finished < cutoff]:
        JOBS.pop(jid, None)


def start_job(kind: str, target: str | None, fn, *args, **kwargs) -> Job:
    _prune_jobs()
    job = Job(kind, target)
    JOBS[job.id] = job

    def runner():
        lock = guest_lock(target) if target else nullcontext()
        with lock:
            try:
                job.result = fn(job, *args, **kwargs)
                job.status = "ok"
                job.write("Fertig.")
            except Exception as exc:  # noqa: BLE001 - Fehler wird an das Panel gemeldet
                job.error = str(exc)
                job.status = "error"
                job.write(f"FEHLER: {exc}")
            finally:
                job.finished = time.time()

    threading.Thread(target=runner, name=f"job-{kind}", daemon=True).start()
    return job
