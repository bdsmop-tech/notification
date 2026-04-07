from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.config import DEFAULT_TZ
from bot.database import SessionLocal
from bot.models import UserSettings


def format_tz_label(tz: tzinfo) -> str:
    if isinstance(tz, ZoneInfo):
        return tz.key
    off = tz.utcoffset(None)
    if off is None:
        return "UTC"
    secs = int(off.total_seconds())
    h = secs // 3600
    if secs % 3600 == 0:
        return f"UTC{'+' if h >= 0 else ''}{h}"
    return str(tz)


async def get_user_zone(user_id: int) -> tzinfo:
    async with SessionLocal() as session:
        row = await session.get(UserSettings, user_id)
        if row is None or not row.timezone:
            return DEFAULT_TZ
        raw = row.timezone
        if raw.startswith("offset:"):
            try:
                h = int(raw.split(":", 1)[1])
            except ValueError:
                return DEFAULT_TZ
            return timezone(timedelta(hours=h))
        try:
            return ZoneInfo(raw)
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


async def set_user_timezone_offset_hours(user_id: int, hours: int) -> tzinfo:
    """Сохраняет смещение от UTC в часах (целое), например +3 или -4."""
    if hours < -12 or hours > 14:
        raise ValueError("offset out of range")
    key = f"offset:{hours}"
    tz = timezone(timedelta(hours=hours))
    async with SessionLocal() as session:
        row = await session.get(UserSettings, user_id)
        if row is None:
            session.add(UserSettings(user_id=user_id, timezone=key))
        else:
            row.timezone = key
        await session.commit()
    return tz


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
