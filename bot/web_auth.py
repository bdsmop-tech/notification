from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update as sql_update

from bot.database import SessionLocal
from bot.models import LoginCode, WebSession


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def issue_login_code(user_id: int) -> str:
    """
    Постоянный код для входа на сайт.
    Генерируется один раз и дальше переиспользуется.
    """
    now = _utcnow()
    exp = now + timedelta(days=3650)
    async with SessionLocal() as session:
        existing = await session.scalar(
            select(LoginCode.code).where(
                LoginCode.user_id == user_id,
                LoginCode.consumed_at.is_(None),
                LoginCode.expires_at > now,
            )
        )
        if existing:
            return str(existing)
        # генерируем новый постоянный код
        for _ in range(10):
            code = f"{secrets.randbelow(1_000_000):06d}"
            exists = await session.scalar(
                select(LoginCode.id).where(LoginCode.code == code, LoginCode.consumed_at.is_(None))
            )
            if not exists:
                session.add(LoginCode(code=code, user_id=user_id, expires_at=exp))
                await session.commit()
                return code
        # fallback: длиннее
        code = secrets.token_hex(3)
        session.add(LoginCode(code=code, user_id=user_id, expires_at=exp))
        await session.commit()
        return code


async def exchange_code_for_session(code: str, *, session_days: int = 30) -> tuple[str, int] | None:
    """Возвращает (raw_token, user_id) или None."""
    now = _utcnow()
    code = code.strip()
    if not code:
        return None
    async with SessionLocal() as session:
        row = await session.scalar(
            select(LoginCode)
            .where(
                LoginCode.code == code,
                LoginCode.consumed_at.is_(None),
                LoginCode.expires_at > now,
            )
            .limit(1)
        )
        if row is None:
            return None
        raw = secrets.token_urlsafe(32)
        token_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        session.add(
            WebSession(
                token_sha256=token_sha,
                user_id=row.user_id,
                expires_at=now + timedelta(days=session_days),
            )
        )
        await session.commit()
        return raw, row.user_id


async def user_id_from_login_code(code: str) -> int | None:
    """Постоянный код из бота: валидная строка в login_codes → user_id."""
    now = _utcnow()
    raw = (code or "").strip()
    if not raw:
        return None
    async with SessionLocal() as session:
        uid = await session.scalar(
            select(LoginCode.user_id).where(
                LoginCode.code == raw,
                LoginCode.consumed_at.is_(None),
                LoginCode.expires_at > now,
            )
        )
        return int(uid) if uid is not None else None


async def user_id_from_session(raw_token: str) -> int | None:
    now = _utcnow()
    if not raw_token:
        return None
    token_sha = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    async with SessionLocal() as session:
        uid = await session.scalar(
            select(WebSession.user_id).where(
                WebSession.token_sha256 == token_sha,
                WebSession.expires_at > now,
            )
        )
        return int(uid) if uid is not None else None


async def revoke_session(raw_token: str) -> None:
    if not raw_token:
        return
    token_sha = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    async with SessionLocal() as session:
        await session.execute(sql_update(WebSession).where(WebSession.token_sha256 == token_sha).values(expires_at=_utcnow()))
        await session.commit()

