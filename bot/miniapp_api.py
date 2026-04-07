"""
HTTP-сервер Mini App: отдаёт статику из miniapp/ и API /api/* с проверкой initData.
Запуск: uvicorn bot.miniapp_api:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from bot.config import BOT_TOKEN
from bot.database import SessionLocal, init_db
from bot.models import Reminder
from bot.tma_validate import validate_telegram_init_data
from bot.user_prefs import format_tz_label, get_user_zone

log = logging.getLogger(__name__)

MINIAPP_ROOT = Path(__file__).resolve().parent.parent / "miniapp"


async def _user_id_from_tma(authorization: str | None) -> int:
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    raw = authorization[4:].strip()
    data = validate_telegram_init_data(raw, BOT_TOKEN)
    if not data or "user" not in data:
        raise HTTPException(status_code=401, detail="Invalid initData")
    try:
        return int(data["user"]["id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid user in initData") from None


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

    @app.get("/api/me")
    async def me(authorization: str | None = Header(None)) -> JSONResponse:
        uid = await _user_id_from_tma(authorization)
        tz = await get_user_zone(uid)
        return JSONResponse({"user_id": uid, "tz_label": format_tz_label(tz)})

    @app.get("/api/reminders/active")
    async def reminders_active(authorization: str | None = Header(None)) -> JSONResponse:
        uid = await _user_id_from_tma(authorization)
        tz = await get_user_zone(uid)
        async with SessionLocal() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.user_id == uid, Reminder.active.is_(True))
                .order_by(Reminder.fire_at.asc())
            )
            rows = result.scalars().all()
        items = []
        for r in rows:
            local = r.fire_at.astimezone(tz)
            items.append(
                {
                    "id": str(r.id),
                    "text": r.text,
                    "fire_at_utc": r.fire_at.isoformat(),
                    "fire_at_local": local.strftime("%d.%m.%Y %H:%M"),
                }
            )
        return JSONResponse({"reminders": items})

    # Статика: /app/... и корень → index.html
    if MINIAPP_ROOT.is_dir():
        app.mount("/app/static", StaticFiles(directory=MINIAPP_ROOT / "static"), name="mini_static")

        @app.get("/", response_model=None)
        async def index() -> FileResponse | JSONResponse:
            index_path = MINIAPP_ROOT / "index.html"
            if not index_path.is_file():
                return JSONResponse({"error": "miniapp/index.html not found"}, status_code=503)
            return FileResponse(index_path, media_type="text/html")

    return app


app = create_app()
