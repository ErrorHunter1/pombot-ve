"""PomBot Panel – Web-Oberfläche und API."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler, tasks
from .config import settings
from .db import Base, SessionLocal, engine
from .routers import auth, extras, guests, nodes, pools, system, users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        system.seed_templates(db)
    tasks.LOOP = asyncio.get_running_loop()
    tasks.mark_stale_tasks()
    poller = asyncio.create_task(tasks.poll_loop())
    backups = asyncio.create_task(scheduler.scheduler_loop())
    yield
    poller.cancel()
    backups.cancel()


app = FastAPI(title="PomBot Panel", version=settings.version, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def csrf_and_headers(request: Request, call_next):
    # Schreibende API-Aufrufe müssen den Header X-PomBot tragen. Fremde Webseiten können diesen
    # Header ohne CORS-Freigabe nicht setzen – das verhindert CSRF.
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-pombot") != "1":
            return JSONResponse({"detail": "CSRF-Schutz: Header fehlt"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="pombot_session",
                   max_age=7 * 24 * 3600, same_site="lax", https_only=settings.https)

for r in (auth.router, users.router, extras.router, guests.router, nodes.router, pools.router, system.router):
    app.include_router(r)

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/console")
def console_page():
    return FileResponse(STATIC / "console.html", headers={"Cache-Control": "no-cache"})
