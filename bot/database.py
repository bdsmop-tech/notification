from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import DATABASE_URL
from bot.models import Base

_async_url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(_async_url, echo=False)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP WITH TIME ZONE"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS spam_until_read BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS quiet_hours_enabled BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS quiet_start_hour INTEGER NOT NULL DEFAULT 23"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS quiet_end_hour INTEGER NOT NULL DEFAULT 7"
            )
        )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
