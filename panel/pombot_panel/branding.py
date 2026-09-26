"""Branding und Suchmaschinen: Name, Meta-Angaben, Indexierung, Tab-Icon, Logo und Vorschaubild.

Alles wird im Adminbereich (Einstellungen → Branding & SEO) gepflegt. Bilder liegen unter
<data_dir>/branding/ und werden öffentlich unter /branding/<art> ausgeliefert (auch die Anmeldeseite und
Link-Vorschauen brauchen sie ohne Login).
"""
import html
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, get_db
from .models import Setting, User
from .security import audit, client_ip, require_admin

DIR = Path(settings.data_dir) / "branding"
KINDS = {"favicon": "Tab-Icon", "logo": "Logo", "og": "Vorschaubild"}
MAX_SIZE = 2 << 20
TYPES = {  # Content-Type -> (Endung, Prüfung der Dateisignatur)
    "image/png": ("png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "image/webp": ("webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
    "image/gif": ("gif", lambda b: b[:6] in (b"GIF87a", b"GIF89a")),
    "image/x-icon": ("ico", lambda b: b[:4] == b"\x00\x00\x01\x00"),
    "image/vnd.microsoft.icon": ("ico", lambda b: b[:4] == b"\x00\x00\x01\x00"),
    "image/svg+xml": ("svg", lambda b: b"<svg" in b[:4096].lower()),
}
DEFAULTS = {
    "name": "PomBot VE",
    "title": "PomBot VE",
    "description": "Virtualisierung für deine Server – VMs und Container verwalten.",
    "keywords": "",
    "indexing": False,  # Admin-Panels gehören normalerweise nicht in Suchmaschinen
    "theme_color": "#2f6fdf",
}
DEFAULT_FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' "
                   "height='32' rx='8' fill='%232f6fdf'/%3E%3Ctext x='16' y='22' font-family='Arial' font-weight='800' "
                   "font-size='17' fill='white' text-anchor='middle'%3EP%3C/text%3E%3C/svg%3E")

router = APIRouter(tags=["branding"])
_cache: dict | None = None


def get() -> dict:
    """Aktuelle Einstellungen (zwischengespeichert, weil jede Seitenauslieferung sie braucht)."""
    global _cache
    if _cache is None:
        with SessionLocal() as db:
            row = db.get(Setting, "branding")
            try:
                stored = json.loads(row.value) if row else {}
            except ValueError:
                stored = {}
        _cache = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS or k == "images"}}
        _cache.setdefault("images", {})
    return _cache


def _save(db: Session, data: dict) -> None:
    global _cache
    row = db.get(Setting, "branding") or Setting(key="branding")
    row.value = json.dumps(data)
    db.add(row)
    db.commit()
    _cache = None


def image_url(kind: str) -> str | None:
    img = get()["images"].get(kind)
    return f"/branding/{kind}?v={img['v']}" if img else None


def public() -> dict:
    """Für die Oberfläche (auch ohne Login): Name und Logo."""
    b = get()
    return {"name": b["name"], "logo": image_url("logo"), "theme_color": b["theme_color"]}


def head_html(request: Request | None = None) -> str:
    """<head>-Teil der Startseite: Titel, Meta, Indexierung, Icons, Link-Vorschau."""
    b = get()
    e = html.escape
    base = settings.base_url
    parts = [f"<title>{e(b['title'] or b['name'])}</title>",
             f'<meta name="description" content="{e(b["description"])}">',
             f'<meta name="theme-color" content="{e(b["theme_color"])}">',
             f'<meta name="robots" content="{"index, follow" if b["indexing"] else "noindex, nofollow"}">',
             f'<meta property="og:title" content="{e(b["title"] or b["name"])}">',
             f'<meta property="og:description" content="{e(b["description"])}">',
             f'<meta property="og:site_name" content="{e(b["name"])}">',
             '<meta property="og:type" content="website">',
             f'<meta property="og:url" content="{e(base)}/">']
    if b["keywords"]:
        parts.append(f'<meta name="keywords" content="{e(b["keywords"])}">')
    fav = image_url("favicon")
    parts.append(f'<link rel="icon" href="{e(fav) if fav else DEFAULT_FAVICON}">')
    if fav:
        parts.append(f'<link rel="apple-touch-icon" href="{e(fav)}">')
    og = image_url("og") or image_url("logo")
    if og:
        parts += [f'<meta property="og:image" content="{e(base + og)}">',
                  '<meta name="twitter:card" content="summary_large_image">']
    return "\n  ".join(parts)


def robots_header() -> dict:
    return {} if get()["indexing"] else {"X-Robots-Tag": "noindex, nofollow"}


# ---------------------------------------------------------------- öffentlich

@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    if get()["indexing"]:
        return "User-agent: *\nAllow: /$\nDisallow: /api/\nDisallow: /console\n"
    return "User-agent: *\nDisallow: /\n"


@router.get("/branding/{kind}")
def branding_image(kind: str):
    img = get()["images"].get(kind)
    if kind not in KINDS or not img:
        raise HTTPException(404)
    path = DIR / f"{kind}.{img['ext']}"
    if not path.exists():
        raise HTTPException(404)
    headers = {"Cache-Control": "public, max-age=86400",
               # SVG kann Skripte enthalten – direkt aufgerufen darf es nichts ausführen
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}
    return FileResponse(path, media_type=img["type"], headers=headers)


# ---------------------------------------------------------------- Admin

class BrandingBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=300)
    keywords: str = Field(default="", max_length=300)
    indexing: bool = False
    theme_color: str = Field(default="#2f6fdf", pattern=r"^#[0-9A-Fa-f]{6}$")


def _view() -> dict:
    b = get()
    return {**{k: b[k] for k in DEFAULTS},
            "images": {k: {"label": label, "url": image_url(k)} for k, label in KINDS.items()},
            "base_url": settings.base_url}


@router.get("/api/admin/branding")
def branding_get(user: User = Depends(require_admin)):
    return _view()


@router.put("/api/admin/branding")
def branding_put(body: BrandingBody, request: Request, user: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    data = {**get(), **body.model_dump()}
    _save(db, data)
    audit(db, user, "branding-update", f"Name {body.name}, Indexierung {'an' if body.indexing else 'aus'}",
          client_ip(request))
    db.commit()
    return _view()


@router.put("/api/admin/branding/{kind}")
async def branding_upload(kind: str, request: Request, user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    """Bild hochladen: Rohdaten im Body, Typ im Content-Type-Header."""
    if kind not in KINDS:
        raise HTTPException(404)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in TYPES:
        raise HTTPException(400, "Erlaubt sind PNG, JPG, WebP, GIF, ICO und SVG")
    body = await request.body()
    if not body or len(body) > MAX_SIZE:
        raise HTTPException(400, "Bild fehlt oder ist größer als 2 MB")
    ext, check = TYPES[ctype]
    if not check(body):
        raise HTTPException(400, "Der Dateiinhalt passt nicht zum Bildtyp")
    DIR.mkdir(parents=True, exist_ok=True)
    for old in DIR.glob(f"{kind}.*"):
        old.unlink()
    (DIR / f"{kind}.{ext}").write_bytes(body)
    data = get()
    data["images"][kind] = {"ext": ext, "type": "image/svg+xml" if ext == "svg" else ctype, "v": int(time.time())}
    _save(db, data)
    audit(db, user, "branding-image", f"{KINDS[kind]} ({ext}, {len(body)} Bytes)", client_ip(request))
    db.commit()
    return _view()


@router.delete("/api/admin/branding/{kind}")
def branding_delete(kind: str, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if kind not in KINDS:
        raise HTTPException(404)
    for old in DIR.glob(f"{kind}.*"):
        old.unlink()
    data = get()
    data["images"].pop(kind, None)
    _save(db, data)
    audit(db, user, "branding-image", f"{KINDS[kind]} entfernt", client_ip(request))
    db.commit()
    return _view()
