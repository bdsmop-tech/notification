from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import DEFAULT_TZ
from bot.database import SessionLocal
from bot.models import UserSettings


async def get_user_zone(user_id: int) -> ZoneInfo:
    async with SessionLocal() as session:
        row = await session.get(UserSettings, user_id)
        if row is None or not row.timezone:
            return DEFAULT_TZ
        try:
            return ZoneInfo(row.timezone)
        except ZoneInfoNotFoundError:
            return DEFAULT_TZ


async def set_user_timezone(user_id: int, tz_name: str) -> ZoneInfo:
    z = ZoneInfo(tz_name)
    async with SessionLocal() as session:
        row = await session.get(UserSettings, user_id)
        if row is None:
            session.add(UserSettings(user_id=user_id, timezone=tz_name))
        else:
            row.timezone = tz_name
        await session.commit()
    return z


async def get_user_settings_row(user_id: int) -> UserSettings | None:
    async with SessionLocal() as session:
        return await session.get(UserSettings, user_id)


async def toggle_quiet_hours(user_id: int) -> bool:
    async with SessionLocal() as session:
        row = await session.get(UserSettings, user_id)
        if row is None:
            session.add(UserSettings(user_id=user_id, quiet_hours_enabled=True))
            await session.commit()
            return True
        row.quiet_hours_enabled = not row.quiet_hours_enabled
        await session.commit()
        return row.quiet_hours_enabled
