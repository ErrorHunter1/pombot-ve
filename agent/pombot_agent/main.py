"""PomBot Agent – läuft als root auf jedem Host und wird ausschließlich vom Panel angesprochen."""
import asyncio
import hmac
import ipaddress
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import console, firewall, host, images, kvm, lxc
from .config import ALLOW_FROM, BACKUP_DIR, TOKEN, VERSION
from .util import JOBS, Busy, CmdError, check_name, check_snap, start_job, try_lock

log = logging.getLogger("pombot.agent")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Firewall-Regeln nach einem Neustart laden und regelmäßig prüfen, ob sie noch aktiv sind
    # (z. B. falls jemand `systemctl restart nftables` ausführt und damit alles leert).
    async def watchdog():
        while True:
            try:
                await asyncio.to_thread(firewall.ensure_loaded)
            except Exception as exc:  # noqa: BLE001
                log.error("Firewall konnte nicht geladen werden: %s", exc)
            await asyncio.sleep(60)
    task = asyncio.create_task(watchdog())
    yield
    task.cancel()


app = FastAPI(title="PomBot Agent", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
BACKUP_RE = re.compile(r"^(pv\d+)-(kvm|lxc)-\d{8}-\d{6}(-auto)?\.tar(\.gz)?$")


def _allowed_ip(ip: str | None) -> bool:
    if not ALLOW_FROM or not ip:
        return not ALLOW_FROM
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or any(addr in net for net in ALLOW_FROM)


def _token_ok(header: str | None) -> bool:
    if not TOKEN or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].encode(), TOKEN.encode())


def auth(request: Request) -> None:
    if not _allowed_ip(request.client.host if request.client else None):
        raise HTTPException(403, "IP nicht erlaubt")
    if not _token_ok(request.headers.get("authorization")):
        raise HTTPException(401, "Ungültiges Token")


@app.exception_handler(CmdError)
async def _cmd_error(_: Request, exc: CmdError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(Busy)
async def _busy(_: Request, exc: Busy):
    return JSONResponse({"detail": str(exc)}, status_code=409)


def module_for(name: str):
    check_name(name)
    if lxc.exists(name):
        return lxc
    if kvm.exists(name):
        return kvm
    raise HTTPException(404, "Gast nicht gefunden")


def job_response(job):
    return {"job": job.id}


# ---------------------------------------------------------------- Allgemein

@app.get("/health")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/host/info", dependencies=[Depends(auth)])
def host_info():
    return host.info()


@app.get("/host/stats", dependencies=[Depends(auth)])
def host_stats():
    return host.stats()


@app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
def job_status(job_id: str, since: int = 0):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job unbekannt (Agent neu gestartet?)")
    return job.as_dict(since)


# ---------------------------------------------------------------- Gäste

class IPSpec(BaseModel):
    address: str
    prefix: int
    gateway: str | None = None
    version: int = 4


class CreateSpec(BaseModel):
    name: str
    type: str = Field(pattern="^(kvm|lxc)$")
    hostname: str = Field(pattern=r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,62})$")
    cores: int = Field(ge=1, le=256)
    memory_mb: int = Field(ge=128, le=4 * 1024 * 1024)
    disk_gb: int = Field(ge=1, le=64 * 1024)
    bridge: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,15}$")
    mac: str = Field(pattern=r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
    ips: list[IPSpec] = []
    dns: list[str] = []
    password: str | None = None
    ssh_keys: list[str] = []
    image_url: str | None = None
    iso_url: str | None = None
    lxc_dist: str | None = None
    lxc_release: str | None = None
    instance_suffix: str = "1"


@app.get("/guests", dependencies=[Depends(auth)])
def guests():
    result = {}
    for mod in (kvm, lxc):
        try:
            result.update(mod.list_guests())
        except CmdError:
            pass
    return result


@app.post("/guests", dependencies=[Depends(auth)])
def guest_create(spec: CreateSpec):
    check_name(spec.name)
    for ip in spec.ips:
        ipaddress.ip_address(ip.address)
    for server in spec.dns:
        ipaddress.ip_address(server)
    data = spec.model_dump()
    data["ips"] = [ip.model_dump() for ip in spec.ips]
    if spec.type == "kvm":
        if not (spec.image_url or spec.iso_url):
            raise HTTPException(400, "image_url oder iso_url erforderlich")
        return job_response(start_job("create", spec.name, kvm.create, data))
    if not (spec.lxc_dist and spec.lxc_release):
        raise HTTPException(400, "lxc_dist und lxc_release erforderlich")
    return job_response(start_job("create", spec.name, lxc.create, data))


@app.get("/guests/{name}", dependencies=[Depends(auth)])
def guest_status(name: str):
    return module_for(name).status(name)


@app.delete("/guests/{name}", dependencies=[Depends(auth)])
def guest_delete(name: str):
    check_name(name)
    mod = lxc if lxc.exists(name) else kvm

    def _delete(job, n):
        result = mod.delete(job, n)
        firewall.remove(n)
        return result
    return job_response(start_job("delete", name, _delete, name))


class ActionBody(BaseModel):
    action: str = Field(pattern="^(start|stop|shutdown|reboot|suspend|resume)$")


@app.post("/guests/{name}/action", dependencies=[Depends(auth)])
def guest_action(name: str, body: ActionBody):
    mod = module_for(name)
    with try_lock(name):
        new_state = mod.action(name, body.action)
    return {"state": new_state}


class ResizeBody(BaseModel):
    cores: int | None = Field(default=None, ge=1, le=256)
    memory_mb: int | None = Field(default=None, ge=128)
    disk_gb: int | None = Field(default=None, ge=1)


@app.post("/guests/{name}/resize", dependencies=[Depends(auth)])
def guest_resize(name: str, body: ResizeBody):
    mod = module_for(name)
    return job_response(start_job("resize", name, mod.resize, name, body.cores, body.memory_mb, body.disk_gb))


class PasswordBody(BaseModel):
    user: str = Field(default="root", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    password: str = Field(min_length=8, max_length=128)


@app.post("/guests/{name}/password", dependencies=[Depends(auth)])
def guest_password(name: str, body: PasswordBody):
    mod = module_for(name)
    with try_lock(name):
        mod.set_password(name, body.user, body.password)
    return {"ok": True}


# ---------------------------------------------------------------- Snapshots

class SnapshotBody(BaseModel):
    name: str
    description: str = ""


@app.get("/guests/{name}/snapshots", dependencies=[Depends(auth)])
def snapshot_list(name: str):
    return module_for(name).snapshots(name)


@app.post("/guests/{name}/snapshots", dependencies=[Depends(auth)])
def snapshot_create(name: str, body: SnapshotBody):
    mod = module_for(name)
    check_snap(body.name)
    return job_response(start_job("snapshot", name, mod.snapshot_create, name, body.name, body.description))


@app.delete("/guests/{name}/snapshots/{snap}", dependencies=[Depends(auth)])
def snapshot_delete(name: str, snap: str):
    mod = module_for(name)
    return job_response(start_job("snapshot-delete", name, mod.snapshot_delete, name, check_snap(snap)))


@app.post("/guests/{name}/snapshots/{snap}/rollback", dependencies=[Depends(auth)])
def snapshot_rollback(name: str, snap: str):
    mod = module_for(name)
    return job_response(start_job("rollback", name, mod.snapshot_rollback, name, check_snap(snap)))


# ---------------------------------------------------------------- Backups

def _backup_file(filename: str):
    m = BACKUP_RE.match(filename)
    path = BACKUP_DIR / filename
    if not m or not path.exists():
        raise HTTPException(404, "Backup nicht gefunden")
    return m, path


@app.get("/backups", dependencies=[Depends(auth)])
def backup_list(name: str | None = None):
    result = []
    for f in sorted(BACKUP_DIR.iterdir(), reverse=True):
        m = BACKUP_RE.match(f.name)
        if m and (not name or m.group(1) == name):
            st = f.stat()
            result.append({"file": f.name, "guest": m.group(1), "type": m.group(2), "auto": bool(m.group(3)),
                           "size": st.st_size, "created": st.st_mtime})
    return result


@app.post("/guests/{name}/backups", dependencies=[Depends(auth)])
def backup_create(name: str, auto: bool = False):
    mod = module_for(name)
    return job_response(start_job("backup", name, mod.backup, name, auto))


@app.delete("/backups/{filename}", dependencies=[Depends(auth)])
def backup_delete(filename: str):
    _, path = _backup_file(filename)
    path.unlink()
    return {"ok": True}


@app.post("/backups/{filename}/restore", dependencies=[Depends(auth)])
def backup_restore(filename: str):
    m, path = _backup_file(filename)
    name, kind = m.group(1), m.group(2)
    mod = lxc if kind == "lxc" else kvm
    return job_response(start_job("restore", name, mod.restore, name, path))


# ---------------------------------------------------------------- Firewall

@app.get("/guests/{name}/firewall", dependencies=[Depends(auth)])
def firewall_get(name: str):
    return firewall.get(check_name(name)) or {"enabled": False}


@app.put("/guests/{name}/firewall", dependencies=[Depends(auth)])
def firewall_put(name: str, cfg: dict):
    return firewall.put(check_name(name), cfg)


@app.delete("/guests/{name}/firewall", dependencies=[Depends(auth)])
def firewall_delete(name: str):
    firewall.remove(check_name(name))
    return {"ok": True}


# ---------------------------------------------------------------- Images

class PullBody(BaseModel):
    url: str
    kind: str = Field(default="cloud", pattern="^(cloud|iso)$")


@app.get("/images", dependencies=[Depends(auth)])
def image_list():
    return images.list_images()


@app.post("/images/pull", dependencies=[Depends(auth)])
def image_pull(body: PullBody):
    return job_response(start_job("pull", None, images.pull, body.url, body.kind))


@app.delete("/images/{image_id}", dependencies=[Depends(auth)])
def image_delete(image_id: str):
    images.delete_image(image_id)
    return {"ok": True}


# ---------------------------------------------------------------- Konsolen

async def _ws_auth(ws: WebSocket) -> bool:
    if not _allowed_ip(ws.client.host if ws.client else None) or not _token_ok(ws.headers.get("authorization")):
        await ws.close(code=4401)
        return False
    return True


@app.websocket("/guests/{name}/console")
async def guest_console(ws: WebSocket, name: str):
    if not await _ws_auth(ws):
        return
    try:
        check_name(name)
    except CmdError:
        await ws.close(code=4404)
        return
    await ws.accept()
    try:
        if lxc.exists(name):
            if lxc.state(name) != "running":
                await ws.send_bytes("Container läuft nicht.\r\n".encode())
                await ws.close()
                return
            await console.pty_session(ws, ["lxc-attach", "-n", name, *lxc.ATTACH,
                                           "--set-var", "TERM=xterm-256color", "--",
                                           "/bin/sh", "-c", "exec bash -l 2>/dev/null || exec sh -l"])
        else:
            await console.vnc_proxy(ws, kvm.vnc_port(name))
    except CmdError as exc:
        await ws.close(code=4400, reason=str(exc)[:120])


@app.websocket("/host/shell")
async def host_shell(ws: WebSocket):
    if not await _ws_auth(ws):
        return
    await ws.accept()
    await console.pty_session(ws, ["/bin/bash", "-l"])
