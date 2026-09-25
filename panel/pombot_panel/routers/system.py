"""Vorlagen, Aufgaben, Audit-Log, Dashboard."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ops
from ..db import get_db
from ..models import AuditLog, Guest, IPPool, Node, Task, Template, User
from ..security import audit, client_ip, current_user, require_admin
from ..tasks import NODE_STATS

router = APIRouter(prefix="/api", tags=["system"])

DEFAULT_TEMPLATES = [
    dict(name="Debian 12 (Bookworm)", type="kvm", source="cloud", os_family="debian", min_disk_gb=4, sort=10,
         url="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"),
    dict(name="Debian 13 (Trixie)", type="kvm", source="cloud", os_family="debian", min_disk_gb=4, sort=11,
         url="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"),
    dict(name="Ubuntu 22.04 LTS", type="kvm", source="cloud", os_family="ubuntu", min_disk_gb=5, sort=20,
         url="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"),
    dict(name="Ubuntu 24.04 LTS", type="kvm", source="cloud", os_family="ubuntu", min_disk_gb=5, sort=21,
         url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"),
    dict(name="Debian 12 (Container)", type="lxc", source="lxc", os_family="debian", min_disk_gb=2, sort=50,
         lxc_dist="debian", lxc_release="bookworm"),
    dict(name="Debian 13 (Container)", type="lxc", source="lxc", os_family="debian", min_disk_gb=2, sort=51,
         lxc_dist="debian", lxc_release="trixie"),
    dict(name="Ubuntu 22.04 (Container)", type="lxc", source="lxc", os_family="ubuntu", min_disk_gb=2, sort=60,
         lxc_dist="ubuntu", lxc_release="jammy"),
    dict(name="Ubuntu 24.04 (Container)", type="lxc", source="lxc", os_family="ubuntu", min_disk_gb=2, sort=61,
         lxc_dist="ubuntu", lxc_release="noble"),
]


def seed_templates(db: Session) -> None:
    if not db.scalar(select(func.count(Template.id))):
        for t in DEFAULT_TEMPLATES:
            db.add(Template(**t))
        db.commit()


# ---------------------------------------------------------------- Vorlagen

def template_dict(t: Template) -> dict:
    return {"id": t.id, "name": t.name, "type": t.type, "source": t.source, "url": t.url,
            "lxc_dist": t.lxc_dist, "lxc_release": t.lxc_release, "os_family": t.os_family,
            "min_disk_gb": t.min_disk_gb, "enabled": t.enabled, "sort": t.sort}


@router.get("/templates")
def list_templates(user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(Template).order_by(Template.sort, Template.name)
    return [template_dict(t) for t in db.scalars(q) if t.enabled or user.is_admin]


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern="^(kvm|lxc)$")
    source: str = Field(pattern="^(cloud|iso|lxc)$")
    url: str | None = Field(default=None, max_length=512)
    lxc_dist: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,32}$")
    lxc_release: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,32}$")
    os_family: str = Field(default="linux", pattern="^(debian|ubuntu|windows|linux)$")
    min_disk_gb: int = Field(default=5, ge=1, le=4096)
    enabled: bool = True
    sort: int = 100


def _check_template(body: TemplateBody) -> None:
    if body.type == "lxc" and (body.source != "lxc" or not body.lxc_dist or not body.lxc_release):
        raise HTTPException(400, "Container-Vorlagen brauchen Distribution und Release")
    if body.type == "kvm":
        if body.source not in ("cloud", "iso"):
            raise HTTPException(400, "VM-Vorlagen sind Cloud-Image oder ISO")
        if not body.url or not body.url.startswith(("https://", "http://")):
            raise HTTPException(400, "Bitte eine http(s)-URL angeben")


@router.post("/templates")
def create_template(body: TemplateBody, request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    _check_template(body)
    t = Template(**body.model_dump())
    db.add(t)
    audit(db, user, "template-create", t.name, client_ip(request))
    db.commit()
    return template_dict(t)


@router.put("/templates/{template_id}")
def update_template(template_id: int, body: TemplateBody, request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    t = db.get(Template, template_id)
    if not t:
        raise HTTPException(404, "Vorlage nicht gefunden")
    _check_template(body)
    for key, value in body.model_dump().items():
        setattr(t, key, value)
    audit(db, user, "template-update", t.name, client_ip(request))
    db.commit()
    return template_dict(t)


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, request: Request, user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    t = db.get(Template, template_id)
    if not t:
        raise HTTPException(404, "Vorlage nicht gefunden")
    audit(db, user, "template-delete", t.name, client_ip(request))
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Aufgaben & Audit

def task_dict(t: Task, with_log: bool = False) -> dict:
    data = {"id": t.id, "action": t.action, "target": t.target, "status": t.status, "user_id": t.user_id,
            "guest_id": t.guest_id, "node_id": t.node_id,
            "started_at": t.started_at.isoformat() + "Z",
            "finished_at": t.finished_at.isoformat() + "Z" if t.finished_at else None}
    if with_log:
        data["log"] = t.log
    return data


@router.get("/tasks")
def list_tasks(limit: int = 100, guest_id: int | None = None, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    q = select(Task).order_by(Task.id.desc()).limit(min(limit, 500))
    if not user.is_admin:
        q = q.where(Task.user_id == user.id)
    if guest_id:
        q = q.where(Task.guest_id == guest_id)
    names = dict(db.execute(select(User.id, User.username)).all())
    return [{**task_dict(t), "user": names.get(t.user_id)} for t in db.scalars(q)]


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = db.get(Task, task_id)
    if not t or (not user.is_admin and t.user_id != user.id):
        raise HTTPException(404, "Aufgabe nicht gefunden")
    return task_dict(t, with_log=True)


@router.get("/audit")
def audit_log(limit: int = 200, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
    return [{"id": a.id, "username": a.username, "action": a.action, "detail": a.detail, "ip": a.ip,
             "created_at": a.created_at.isoformat() + "Z"} for a in db.scalars(q)]


# ---------------------------------------------------------------- Dashboard

@router.get("/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    mine = list(db.scalars(select(Guest).where(Guest.owner_id == user.id)))
    data = {
        "my": {"total": len(mine), "running": sum(g.power == "running" for g in mine),
               "quota": ops.quota(user), "usage": ops.usage(db, user)},
    }
    if user.is_admin:
        nodes = list(db.scalars(select(Node)))
        cluster = {"cpu_cores": 0, "memory_total": 0, "memory_used": 0, "disk_total": 0, "disk_used": 0,
                   "cpu_weighted": 0.0}
        for n in nodes:
            s = NODE_STATS.get(n.id)
            if not s:
                continue
            cores = n.info.get("cpu_cores") or 1
            cluster["cpu_cores"] += cores
            cluster["cpu_weighted"] += s["cpu"] * cores
            for key in ("memory_total", "memory_used", "disk_total", "disk_used"):
                cluster[key] += s[key]
        cluster["cpu"] = round(cluster.pop("cpu_weighted") / cluster["cpu_cores"], 1) if cluster["cpu_cores"] else 0
        all_guests = list(db.scalars(select(Guest)))
        data["cluster"] = {
            **cluster,
            "nodes": len(nodes), "nodes_online": sum(n.status == "online" for n in nodes),
            "guests": len(all_guests), "guests_running": sum(g.power == "running" for g in all_guests),
            "users": db.scalar(select(func.count(User.id))),
            "users_pending": db.scalar(select(func.count(User.id)).where(User.active.is_(False))),
            "pools": db.scalar(select(func.count(IPPool.id))),
        }
    return data
