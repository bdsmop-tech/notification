"""Ссылка на Application python-telegram-bot в процессе, где бот и HTTP запущены вместе."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

_ptb_app: Application | None = None


def set_ptb_application(application: Application) -> None:
    global _ptb_app
    _ptb_app = application


def get_ptb_application() -> Application | None:
    return _ptb_app


def get_ptb_bot():
    """Возвращает Bot или None (например, при отдельном uvicorn без polling)."""
    if _ptb_app is None:
        return None
    return _ptb_app.bot
