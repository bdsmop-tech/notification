"""
HTTP-сервер Mini App: статика miniapp/ и API /api/* с проверкой initData.

В проде с ботом в одном процессе HTTP поднимается из bot.__main__ (post_init + uvicorn).
Отдельно: uvicorn bot.miniapp_api:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bot.database import init_db
from bot.miniapp_routes import router as miniapp_router

log = logging.getLogger(__name__)

MINIAPP_ROOT = Path(__file__).resolve().parent.parent / "miniapp"


def create_app() -> FastAPI:
    app = FastAPI(title="Reminder Mini App API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db()
        log.info("Mini App API ready, static root=%s", MINIAPP_ROOT)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(miniapp_router)

    if MINIAPP_ROOT.is_dir():
        app.mount("/app/static", StaticFiles(directory=MINIAPP_ROOT / "static"), name="mini_static")

        @app.get("/", response_model=None)
        async def index() -> FileResponse | JSONResponse:
            index_path = MINIAPP_ROOT / "index.html"
            if not index_path.is_file():
                return JSONResponse({"error": "miniapp/index.html not found"}, status_code=503)
            return FileResponse(index_path, media_type="text/html")

        @app.get("/web", response_model=None)
        async def web_login_page() -> FileResponse | JSONResponse:
            web_path = MINIAPP_ROOT / "web.html"
            if not web_path.is_file():
                return JSONResponse({"error": "miniapp/web.html not found"}, status_code=503)
            return FileResponse(web_path, media_type="text/html")

        @app.get("/offline", response_model=None)
        async def offline_page() -> FileResponse | JSONResponse:
            p = MINIAPP_ROOT / "offline.html"
            if not p.is_file():
                return JSONResponse({"error": "miniapp/offline.html not found"}, status_code=503)
            return FileResponse(p, media_type="text/html")

        @app.get("/manifest.webmanifest", response_model=None)
        async def manifest() -> FileResponse | JSONResponse:
            p = MINIAPP_ROOT / "manifest.webmanifest"
            if not p.is_file():
                return JSONResponse({"error": "miniapp/manifest.webmanifest not found"}, status_code=503)
            return FileResponse(p, media_type="application/manifest+json")

        @app.get("/sw.js", response_model=None)
        async def sw() -> FileResponse | JSONResponse:
            p = MINIAPP_ROOT / "sw.js"
            if not p.is_file():
                return JSONResponse({"error": "miniapp/sw.js not found"}, status_code=503)
            return FileResponse(p, media_type="application/javascript")

    return app


app = create_app()
