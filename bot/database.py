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
                "ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS profile_name VARCHAR(64)"
            )
        )
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_user_settings_profile_name_lower
                ON user_settings (LOWER(profile_name))
                WHERE profile_name IS NOT NULL
                """
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
        await conn.execute(
            text(
                "ALTER TABLE user_settings ALTER COLUMN timezone TYPE VARCHAR(128)"
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS friend_requests (
                    id SERIAL PRIMARY KEY,
                    from_user_id BIGINT NOT NULL,
                    to_user_id BIGINT NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    responded_at TIMESTAMPTZ NULL
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_requests_from_user_id ON friend_requests(from_user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_requests_to_user_id ON friend_requests(to_user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_requests_status ON friend_requests(status)"))
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS friendships (
                    id SERIAL PRIMARY KEY,
                    user_low_id BIGINT NOT NULL,
                    user_high_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_friendships_pair ON friendships(user_low_id, user_high_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friendships_user_low_id ON friendships(user_low_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friendships_user_high_id ON friendships(user_high_id)"))
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS friend_reminders (
                    id SERIAL PRIMARY KEY,
                    sender_user_id BIGINT NOT NULL,
                    receiver_user_id BIGINT NOT NULL,
                    reminder_id UUID NOT NULL,
                    fire_at_sender_tz VARCHAR(32) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'scheduled',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    delivered_at TIMESTAMPTZ NULL,
                    closed_at TIMESTAMPTZ NULL
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_reminders_sender_user_id ON friend_reminders(sender_user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_reminders_receiver_user_id ON friend_reminders(receiver_user_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_reminders_reminder_id ON friend_reminders(reminder_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_friend_reminders_status ON friend_reminders(status)"))


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
