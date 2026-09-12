"""SamaConcept – GeoProjects : point d'entrée FastAPI.

Lancement (Docker ou local) : uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import html
import logging
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.ai_agent import AIAgent
from app.config import Config, load_config
from app.database import Database
from app.routes import router
from app.sync import SyncWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("samaconcept")

BASE_DIR = Path(__file__).resolve().parent


def create_app(config_path: Optional[str] = None) -> FastAPI:
    """Fabrique l'application. ``config_path`` surcharge config.yaml (tests)."""
    config: Config = load_config(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(config.app.db_path)
        agent = AIAgent(config.llm_agent, api_key_override=config.effective_api_key)
        worker = SyncWorker(config, db, agent)
        app.state.config = config
        app.state.db = db
        app.state.agent = agent
        app.state.sync = worker
        logger.info(
            "Démarrage — racine projets : %s (intervalle sync : %s min)",
            config.app.projects_root_dir, config.app.sync_interval_minutes,
        )
        tasks: list[asyncio.Task] = []
        # Synchro d'indexation au démarrage : rapide et sans LLM.
        await asyncio.to_thread(worker.sync_once)
        tasks.append(asyncio.create_task(worker.run()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            db.close()

    app = FastAPI(
        title="SamaConcept – GeoProjects",
        description="Cartographie 3D et moteur de recherche multi-critères des projets du bureau d'études.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)

    static_dir = BASE_DIR / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Accès HTTP en lecture seule aux dossiers projets → liens cliquables
    # (https://<serveur>/projects/<dossier>/), avec listing des dossiers.
    app.mount(
        "/projects",
        ProjectsFileServer(config.app.projects_root_dir),
        name="projects",
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok"}

    return app


class ProjectsFileServer:
    """Sert les dossiers projets en lecture seule, avec listing HTML des
    dossiers (remplace le listing retiré de Starlette StaticFiles)."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    @staticmethod
    def _within(root: Path, full: Path) -> bool:
        try:
            full.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size < 1024:
            return f"{size} o"
        if size < 1048576:
            return f"{size / 1024:.1f} Ko"
        return f"{size / 1048576:.1f} Mo"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return

        rel_path = scope["path"]
        root_path = scope.get("root_path", "")
        if root_path and rel_path.startswith(root_path):
            rel_path = rel_path[len(root_path):]
        rel = rel_path.lstrip("/")
        full = (self.root / rel).resolve()
        if not self._within(self.root, full) or not full.exists():
            response = Response(b'{"detail":"Not Found"}', status_code=404, media_type="application/json")
            await response(scope, receive, send)
            return

        if full.is_dir():
            await self._send_listing(scope, receive, send, full, rel)
            return
        await FileResponse(full)(scope, receive, send)

    async def _send_listing(self, scope, receive, send, folder: Path, rel: str) -> None:
        entries = []
        for p in sorted(folder.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            stat = p.stat()
            entries.append({
                "name": p.name,
                "href": urllib.parse.quote(p.name),
                "dir": p.is_dir(),
                "size": "" if p.is_dir() else self._fmt_size(stat.st_size),
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
            })

        rows = ['<li><a class="dir" href="../">📁 ../</a></li>'] if rel else []
        for e in entries:
            icon = "📁" if e["dir"] else "📄"
            suffix = "/" if e["dir"] else ""
            rows.append(
                f'<li><a class={"dir" if e["dir"] else "file"} href="{e["href"]}{suffix}">'
                f'{icon} {html.escape(e["name"])}</a>'
                f'<span class="meta">{e["size"]} · {e["mtime"]}</span></li>'
            )

        title = html.escape(rel or "racine des projets")
        body = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SamaConcept · {title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0b1020; color: #e8eefc; margin: 0; padding: 1rem; }}
  h1 {{ font-size: 1.1rem; color: #e8eefc; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ display: flex; justify-content: space-between; gap: 1rem; padding: .45rem .6rem; border-bottom: 1px solid #24304d; }}
  a {{ color: #7fb2ff; text-decoration: none; word-break: break-all; }}
  a.dir {{ font-weight: 600; }}
  .meta {{ color: #8fa0c4; font-size: .8rem; white-space: nowrap; }}
</style>
</head>
<body>
<h1>📁 Projets — {title}</h1>
<ul>
{''.join(rows)}
</ul>
</body>
</html>"""
        response = Response(body.encode("utf-8"), media_type="text/html; charset=utf-8")
        await response(scope, receive, send)


app = create_app()