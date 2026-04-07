"""Тихие часы в локальном времени пользователя."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def in_quiet_window(local_now: datetime, start_h: int, end_h: int) -> bool:
    h = local_now.hour
    if start_h > end_h:
        return h >= start_h or h < end_h
    return start_h <= h < end_h


def next_quiet_end_utc(
    now_utc: datetime,
    tz: ZoneInfo,
    start_h: int,
    end_h: int,
) -> datetime | None:
    """Если сейчас тихое окно — UTC момент, когда можно слать (первый end_h:00 после now)."""
    local = now_utc.astimezone(tz)
    if not in_quiet_window(local, start_h, end_h):
        return None
    h = local.hour
    if start_h > end_h:
        if h < end_h:
            nxt = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
        else:
            d = local.date() + timedelta(days=1)
            nxt = datetime.combine(d, time(end_h, 0), tzinfo=tz)
    else:
        nxt = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
        if nxt <= local:
            nxt += timedelta(days=1)
    return nxt.astimezone(timezone.utc)
