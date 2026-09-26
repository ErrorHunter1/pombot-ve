"""Adminbereich: Einstellungen, Cloudflare-DNS, Panel-Domain mit Let's-Encrypt-Zertifikat."""
import asyncio
import ipaddress
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import acme, runtime, tasks
from ..cloudflare import RECORD_TYPES, Cloudflare, CloudflareError
from ..config import settings
from ..db import get_db, session_scope
from ..models import User
from ..security import audit, client_ip, guest_for, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _cf() -> Cloudflare:
    try:
        return Cloudflare(settings.cloudflare_token)
    except CloudflareError as exc:
        raise HTTPException(400, str(exc))


def _cf_call(fn):
    try:
        return fn()
    except CloudflareError as exc:
        raise HTTPException(400, f"Cloudflare: {exc}")


# ---------------------------------------------------------------- Einstellungen

@router.get("/settings")
def get_settings(user: User = Depends(require_admin)):
    data = runtime.view()
    data["discord_redirect_uri"] = settings.discord_redirect_uri
    data["base_url"] = settings.base_url
    return data


@router.put("/settings")
def put_settings(body: dict, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    # panel_domain/acme_enabled nur über /domain, api_enabled nur über /api/admin/api (nicht per Token änderbar)
    body = {k: v for k, v in body.items() if k not in ("panel_domain", "acme_enabled", "api_enabled")}
    changed = runtime.save(db, body)
    audit(db, user, "settings-update", ", ".join(changed), client_ip(request))
    db.commit()
    return get_settings(user)


# ---------------------------------------------------------------- Cloudflare DNS

@router.post("/cloudflare/verify")
def cf_verify(user: User = Depends(require_admin)):
    return _cf_call(lambda: _cf().verify())


@router.get("/cloudflare/zones")
def cf_zones(user: User = Depends(require_admin)):
    return _cf_call(lambda: _cf().zones())


@router.get("/cloudflare/zones/{zone_id}/records")
def cf_records(zone_id: str, user: User = Depends(require_admin)):
    return _cf_call(lambda: _cf().records(zone_id))


class RecordBody(BaseModel):
    type: str = Field(pattern="^(" + "|".join(RECORD_TYPES) + ")$")
    name: str = Field(min_length=1, max_length=253)
    content: str = Field(min_length=1, max_length=4096)
    ttl: int = Field(default=1, ge=1, le=86400)  # 1 = automatisch
    proxied: bool = False
    priority: int | None = Field(default=None, ge=0, le=65535)
    comment: str = Field(default="", max_length=100)


def _record(body: RecordBody) -> dict:
    data = body.model_dump(exclude_none=True)
    if body.type not in ("A", "AAAA", "CNAME"):
        data["proxied"] = False  # Proxy gibt es nur für A/AAAA/CNAME
    if body.type in ("A", "AAAA"):
        try:
            ip = ipaddress.ip_address(body.content.strip())
        except ValueError:
            raise HTTPException(400, f"{body.content} ist keine gültige IP-Adresse")
        if (ip.version == 4) != (body.type == "A"):
            raise HTTPException(400, "A-Einträge brauchen IPv4, AAAA-Einträge IPv6")
    if not data.get("comment"):
        data.pop("comment", None)
    return data


@router.post("/cloudflare/zones/{zone_id}/records")
def cf_create(zone_id: str, body: RecordBody, request: Request, user: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    rec = _cf_call(lambda: _cf().create(zone_id, _record(body)))
    audit(db, user, "dns-create", f"{body.type} {body.name} -> {body.content}", client_ip(request))
    db.commit()
    return rec


@router.put("/cloudflare/zones/{zone_id}/records/{record_id}")
def cf_update(zone_id: str, record_id: str, body: RecordBody, request: Request, user: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    rec = _cf_call(lambda: _cf().update(zone_id, record_id, _record(body)))
    audit(db, user, "dns-update", f"{body.type} {body.name} -> {body.content}", client_ip(request))
    db.commit()
    return rec


@router.delete("/cloudflare/zones/{zone_id}/records/{record_id}")
def cf_delete(zone_id: str, record_id: str, request: Request, user: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    _cf_call(lambda: _cf().delete(zone_id, record_id))
    audit(db, user, "dns-delete", record_id, client_ip(request))
    db.commit()
    return {"ok": True}


class GuestDnsBody(BaseModel):
    hostname: str = Field(min_length=3, max_length=253)
    proxied: bool = False


@router.post("/guests/{guest_id}/dns")
def guest_dns(guest_id: int, body: GuestDnsBody, request: Request, user: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    """Legt A/AAAA-Einträge in Cloudflare für die IPs eines Servers an (oder aktualisiert sie)."""
    g = guest_for(db, user, guest_id)
    name = body.hostname.strip().lower().rstrip(".")
    if not DOMAIN_RE.match(name):
        raise HTTPException(400, "Bitte einen vollständigen Hostnamen angeben, z. B. web.deinedomain.de")
    if not g.ips:
        raise HTTPException(400, "Der Server hat keine feste IP-Adresse")
    cf = _cf()
    created = []
    for ip in g.ips:
        rtype = "A" if ipaddress.ip_address(ip.address).version == 4 else "AAAA"
        rec = _cf_call(lambda: cf.upsert(name, rtype, ip.address, body.proxied, comment=f"PomBot VE #{g.vmid}"))
        created.append({"type": rtype, "name": rec["name"], "content": rec["content"]})
    audit(db, user, "guest-dns", f"#{g.vmid} {name}", client_ip(request))
    db.commit()
    return created


# ---------------------------------------------------------------- Panel-Domain & HTTPS

def _public_ip() -> str | None:
    try:
        text = httpx.get("https://1.1.1.1/cdn-cgi/trace", timeout=5).text
        return next((line[3:] for line in text.splitlines() if line.startswith("ip=")), None)
    except httpx.HTTPError:
        return None


@router.get("/domain")
async def get_domain(user: User = Depends(require_admin)):
    cert = acme.cert_info(runtime.tls_dir() / "fullchain.pem")
    return {
        "base_url": settings.base_url, "port": settings.port, "domain": settings.panel_domain,
        "acme_enabled": settings.acme_enabled, "acme_email": settings.acme_email,
        "cloudflare": bool(settings.cloudflare_token), "certificate": cert,
        "public_ip": await asyncio.to_thread(_public_ip), "restart_supported": runtime.restart_supported(),
    }


class DomainBody(BaseModel):
    domain: str = Field(min_length=3, max_length=253)
    port: int = Field(default=443, ge=1, le=65535)
    email: str = Field(default="", max_length=255)
    create_dns: bool = True
    dns_ip: str | None = None
    staging: bool = False


def _setup_domain(task_id: int, body: DomainBody, domain: str) -> None:
    log = lambda msg: tasks.task_log(task_id, msg)  # noqa: E731
    cf = Cloudflare(settings.cloudflare_token)
    if body.create_dns:
        ip = body.dns_ip or _public_ip()
        if not ip:
            raise CloudflareError("Öffentliche IP nicht ermittelbar – bitte angeben")
        rtype = "A" if ipaddress.ip_address(ip).version == 4 else "AAAA"
        cf.upsert(domain, rtype, ip, proxied=False, comment="PomBot VE Panel")
        log(f"DNS: {rtype} {domain} → {ip} (Cloudflare, nicht proxied)")
    acme.issue(domain, body.email, cf, runtime.tls_dir(), log, staging=body.staging)
    base_url = f"https://{domain}" + ("" if body.port == 443 else f":{body.port}")
    runtime.write_runtime_env(base_url, body.port, runtime.tls_dir() / "fullchain.pem",
                              runtime.tls_dir() / "privkey.pem")
    with session_scope() as db:
        runtime.save(db, {"panel_domain": domain, "acme_enabled": True, "acme_email": body.email,
                          "acme_staging": body.staging})
    log(f"Neue Adresse des Panels: {base_url}")
    log(f"Discord-Redirect bitte anpassen auf: {base_url}/api/auth/discord/callback")
    if runtime.schedule_restart(tasks.LOOP):
        log("Panel startet in wenigen Sekunden neu …")
    else:
        log("Bitte das Panel neu starten: systemctl restart pombot-panel")


@router.post("/domain")
def set_domain(body: DomainBody, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    domain = body.domain.strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain):
        raise HTTPException(400, "Ungültige Domain, Beispiel: panel.deinedomain.de")
    if not settings.cloudflare_token:
        raise HTTPException(400, "Bitte zuerst unter „Cloudflare“ einen API-Token hinterlegen")
    if body.port < 1024 and body.port != 443:
        raise HTTPException(400, "Erlaubt sind Port 443 oder Ports ab 1024")
    if body.dns_ip:
        try:
            ipaddress.ip_address(body.dns_ip)
        except ValueError:
            raise HTTPException(400, "Ungültige IP-Adresse für den DNS-Eintrag")
    task = tasks.create_task(db, user.id, "domain", domain)
    audit(db, user, "panel-domain", f"{domain}:{body.port}", client_ip(request))
    db.commit()

    async def run():
        await asyncio.to_thread(_setup_domain, task.id, body, domain)
    tasks.submit(tasks.run_task(task.id, run()))
    return {"task_id": task.id}


@router.post("/domain/reset")
def reset_domain(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Zurück zur Erreichbarkeit per IP mit dem selbstsignierten Zertifikat."""
    runtime.reset_runtime_env()
    runtime.save(db, {"panel_domain": "", "acme_enabled": False})
    audit(db, user, "panel-domain-reset", "", client_ip(request))
    db.commit()
    restarting = runtime.schedule_restart(tasks.LOOP)
    return {"restarting": restarting}


def renew_if_needed() -> None:
    """Wird regelmäßig vom Scheduler aufgerufen: erneuert das Zertifikat 30 Tage vor Ablauf."""
    if not (settings.acme_enabled and settings.panel_domain and settings.cloudflare_token):
        return
    info = acme.cert_info(runtime.tls_dir() / "fullchain.pem")
    if info and info["days_left"] > 30:
        return
    with session_scope() as db:
        task = tasks.create_task(db, None, "domain", f"{settings.panel_domain} (Verlängerung)")
        task_id = task.id
    try:
        acme.issue(settings.panel_domain, settings.acme_email, Cloudflare(settings.cloudflare_token),
                   runtime.tls_dir(), lambda m: tasks.task_log(task_id, m), staging=settings.acme_staging)
        tasks.task_finish(task_id, True, "Zertifikat verlängert – Panel startet neu.")
        runtime.schedule_restart(tasks.LOOP)
    except Exception as exc:  # noqa: BLE001
        tasks.task_finish(task_id, False, f"FEHLER: {exc}")

