"""REST API Mini App: тот же функционал, что у бота (напоминания, пояс, тихие часы)."""

from __future__ import annotations

import calendar as cal_module
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, nulls_last, select, update

from bot.config import BOT_TOKEN, MIN_SPAM_INTERVAL_SECONDS, READ_ACK_INTERVAL_SECONDS
from bot.database import SessionLocal
from bot.models import Reminder
from bot.reminder_worker import stop_reminder_by_id
from bot.time_parse import parse_time_one_line, parse_trailing_text_and_time
from bot.tma_validate import validate_telegram_init_data
from bot.user_prefs import (
    format_tz_label,
    get_user_settings_row,
    get_user_zone,
    set_user_timezone_offset_hours,
    toggle_quiet_hours,
)

PAGE_SIZE = 5

router = APIRouter(prefix="/api", tags=["miniapp"])


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def require_tma_user(authorization: str | None = Header(None)) -> int:
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


TmaUser = Annotated[int, Depends(require_tma_user)]


def _spam_variant_to_db(
    variant: str,
    custom_seconds: int,
) -> tuple[int, bool]:
    if variant == "once":
        return 0, False
    if variant == "until_read":
        return READ_ACK_INTERVAL_SECONDS, True
    if variant == "i30":
        return 30, False
    if variant == "i60":
        return 60, False
    if variant == "i120":
        return 120, False
    if variant == "custom":
        s = custom_seconds
        if s < 0:
            raise HTTPException(status_code=400, detail="custom interval must be >= 0")
        if s > 0 and s < MIN_SPAM_INTERVAL_SECONDS:
            s = MIN_SPAM_INTERVAL_SECONDS
        return s, False
    raise HTTPException(status_code=400, detail="unknown spam_variant")


def _variant_from_reminder(r: Reminder) -> str:
    if r.spam_until_read:
        return "until_read"
    if not r.spam_interval_seconds:
        return "once"
    x = r.spam_interval_seconds
    if x == 30:
        return "i30"
    if x == 60:
        return "i60"
    if x == 120:
        return "i120"
    return "custom"


def _serialize_reminder(r: Reminder, tz) -> dict:
    local = r.fire_at.astimezone(tz)
    closed_local = r.closed_at.astimezone(tz) if r.closed_at else None
    return {
        "id": str(r.id),
        "text": r.text,
        "fire_at_utc": r.fire_at.isoformat(),
        "fire_at_local": local.strftime("%d.%m.%Y %H:%M"),
        "date_local": local.strftime("%Y-%m-%d"),
        "time_local": local.strftime("%H:%M"),
        "active": r.active,
        "spam_variant": _variant_from_reminder(r),
        "spam_interval_seconds": r.spam_interval_seconds,
        "spam_until_read": r.spam_until_read,
        "closed_at_local": closed_local.strftime("%d.%m.%Y %H:%M") if closed_local else None,
    }


async def _offset_hours(user_id: int) -> int | None:
    row = await get_user_settings_row(user_id)
    if row is None or not row.timezone or not row.timezone.startswith("offset:"):
        return None
    try:
        return int(row.timezone.split(":", 1)[1])
    except ValueError:
        return None


@router.get("/config")
async def api_config() -> dict:
    return {
        "min_spam_interval_seconds": MIN_SPAM_INTERVAL_SECONDS,
        "read_ack_interval_seconds": READ_ACK_INTERVAL_SECONDS,
        "page_size": PAGE_SIZE,
    }


@router.get("/me")
async def api_me(user_id: TmaUser) -> dict:
    tz = await get_user_zone(user_id)
    row = await get_user_settings_row(user_id)
    off = await _offset_hours(user_id)
    return {
        "user_id": user_id,
        "tz_label": format_tz_label(tz),
        "offset_hours": off,
        "quiet_hours_enabled": bool(row and row.quiet_hours_enabled),
        "min_spam_interval_seconds": MIN_SPAM_INTERVAL_SECONDS,
        "read_ack_interval_seconds": READ_ACK_INTERVAL_SECONDS,
    }


class TimezoneBody(BaseModel):
    offset_hours: int = Field(ge=-12, le=14)


@router.post("/me/timezone")
async def api_set_timezone(body: TimezoneBody, user_id: TmaUser) -> dict:
    try:
        tz = await set_user_timezone_offset_hours(user_id, body.offset_hours)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid offset") from None
    return {"tz_label": format_tz_label(tz), "offset_hours": body.offset_hours}


@router.post("/me/quiet-hours/toggle")
async def api_toggle_quiet(user_id: TmaUser) -> dict:
    on = await toggle_quiet_hours(user_id)
    return {"quiet_hours_enabled": on}


@router.get("/calendar/{year}/{month}")
async def api_calendar(year: int, month: int, user_id: TmaUser) -> dict:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="bad month")
    c = cal_module.Calendar(firstweekday=0)
    weeks = c.monthdatescalendar(year, month)
    grid: list[list[int | None]] = []
    for week in weeks:
        grid.append([d.day if d.month == month else None for d in week])
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    ru = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    return {"year": year, "month": month, "month_label": f"{ru[month]} {year}", "weekday_names": names, "weeks": grid}


@router.get("/reminders/active")
async def reminders_active(user_id: TmaUser, page: int = 0) -> dict:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.active.is_(True),
            )
        )
        total = int(count or 0)
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(max(page, 0), pages - 1)
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.active.is_(True))
            .order_by(Reminder.fire_at.asc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        rows = result.scalars().all()
    return {
        "reminders": [_serialize_reminder(r, tz) for r in rows],
        "page": page,
        "pages": pages,
        "total": total,
    }


@router.get("/reminders/today")
async def reminders_today(user_id: TmaUser) -> dict:
    tz = await get_user_zone(user_id)
    today = _utcnow().astimezone(tz).date()
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.active.is_(True))
            .order_by(Reminder.fire_at.asc())
        )
        all_rows = result.scalars().all()
    rows = [r for r in all_rows if r.fire_at.astimezone(tz).date() == today]
    return {"reminders": [_serialize_reminder(r, tz) for r in rows]}


@router.get("/reminders/history")
async def reminders_history(user_id: TmaUser, page: int = 0) -> dict:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.active.is_(False),
            )
        )
        total = int(count or 0)
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(max(page, 0), pages - 1)
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.active.is_(False))
            .order_by(nulls_last(Reminder.closed_at.desc()), Reminder.fire_at.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        rows = result.scalars().all()
    return {
        "reminders": [_serialize_reminder(r, tz) for r in rows],
        "page": page,
        "pages": pages,
        "total": total,
    }


@router.get("/reminders/{reminder_id}")
async def reminder_one(reminder_id: UUID, user_id: TmaUser) -> dict:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or r.user_id != user_id:
            raise HTTPException(status_code=404, detail="not found")
    return _serialize_reminder(r, tz)


SpamVariant = Literal["once", "until_read", "i30", "i60", "i120", "custom"]


class CreateReminderBody(BaseModel):
    text: str | None = None
    date: str | None = None
    time: str | None = None
    quick_line: str | None = None
    from_history_id: str | None = None
    spam_variant: SpamVariant = "once"
    spam_interval_seconds: int = 0


def _parse_local_date(s: str) -> date:
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date, use YYYY-MM-DD") from None


@router.post("/reminders")
async def reminder_create(body: CreateReminderBody, user_id: TmaUser) -> dict:
    tz = await get_user_zone(user_id)
    text = (body.text or "").strip()
    if body.from_history_id:
        try:
            hid = UUID(body.from_history_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad from_history_id") from None
        async with SessionLocal() as session:
            hr = await session.get(Reminder, hid)
            if hr is None or hr.user_id != user_id or hr.active:
                raise HTTPException(status_code=404, detail="history item not found")
            text = hr.text
    if body.quick_line:
        one = parse_trailing_text_and_time(body.quick_line.strip())
        if not one:
            raise HTTPException(
                status_code=400,
                detail='quick_line: ожидается «текст 16 43» в конце два числа',
            )
        text_part, t = one
        text = text_part
        today = _utcnow().astimezone(tz).date()
        local_dt = datetime.combine(today, t, tzinfo=tz)
        fire_at = local_dt.astimezone(timezone.utc)
        if fire_at <= _utcnow():
            raise HTTPException(status_code=400, detail="это время сегодня уже прошло")
    else:
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        if not body.date or not body.time:
            raise HTTPException(status_code=400, detail="date and time required unless quick_line")
        d = _parse_local_date(body.date)
        t = parse_time_one_line(body.time)
        if t is None:
            raise HTTPException(status_code=400, detail="bad time, use 16 43 or 16:43")
        local_dt = datetime.combine(d, t, tzinfo=tz)
        fire_at = local_dt.astimezone(timezone.utc)
        if fire_at <= _utcnow():
            raise HTTPException(status_code=400, detail="время должно быть в будущем")

    spam, until_read = _spam_variant_to_db(body.spam_variant, body.spam_interval_seconds)
    chat_id = user_id

    async with SessionLocal() as session:
        r = Reminder(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            fire_at=fire_at,
            spam_interval_seconds=spam,
            spam_until_read=until_read,
            active=True,
        )
        session.add(r)
        await session.commit()
        await session.refresh(r)
    return _serialize_reminder(r, tz)


class PatchReminderBody(BaseModel):
    text: str | None = None
    date: str | None = None
    time: str | None = None
    spam_variant: SpamVariant | None = None
    spam_interval_seconds: int = 0


@router.patch("/reminders/{reminder_id}")
async def reminder_patch(reminder_id: UUID, body: PatchReminderBody, user_id: TmaUser) -> dict:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or r.user_id != user_id or not r.active:
            raise HTTPException(status_code=404, detail="not found")
        if body.text is not None:
            r.text = body.text.strip()
        if body.date is not None and body.time is not None:
            d = _parse_local_date(body.date)
            t = parse_time_one_line(body.time)
            if t is None:
                raise HTTPException(status_code=400, detail="bad time")
            local_dt = datetime.combine(d, t, tzinfo=tz)
            fire_at = local_dt.astimezone(timezone.utc)
            if fire_at <= _utcnow():
                raise HTTPException(status_code=400, detail="время должно быть в будущем")
            r.fire_at = fire_at
        elif body.date is not None or body.time is not None:
            raise HTTPException(status_code=400, detail="укажите и date, и time")
        if body.spam_variant is not None:
            spam, until_read = _spam_variant_to_db(body.spam_variant, body.spam_interval_seconds)
            r.spam_interval_seconds = spam
            r.spam_until_read = until_read
        await session.commit()
        await session.refresh(r)
    return _serialize_reminder(r, tz)


class PatchSpamBody(BaseModel):
    spam_variant: SpamVariant
    spam_interval_seconds: int = 0


@router.patch("/reminders/{reminder_id}/spam")
async def reminder_patch_spam(reminder_id: UUID, body: PatchSpamBody, user_id: TmaUser) -> dict:
    """Только режим повтора (как кнопки ens: в боте)."""
    tz = await get_user_zone(user_id)
    spam, until_read = _spam_variant_to_db(body.spam_variant, body.spam_interval_seconds)
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or r.user_id != user_id or not r.active:
            raise HTTPException(status_code=404, detail="not found")
        r.spam_interval_seconds = spam
        r.spam_until_read = until_read
        await session.commit()
        await session.refresh(r)
    return _serialize_reminder(r, tz)


@router.post("/reminders/{reminder_id}/archive")
async def reminder_archive(reminder_id: UUID, user_id: TmaUser) -> dict:
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or r.user_id != user_id:
            raise HTTPException(status_code=404, detail="not found")
        await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(active=False, closed_at=now)
        )
        await session.commit()
    return {"ok": True}


class SnoozeBody(BaseModel):
    minutes: int = Field(ge=1, le=10080)


@router.post("/reminders/{reminder_id}/snooze")
async def reminder_snooze(reminder_id: UUID, body: SnoozeBody, user_id: TmaUser) -> dict:
    tz = await get_user_zone(user_id)
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or r.user_id != user_id or not r.active:
            raise HTTPException(status_code=404, detail="not found")
        r.fire_at = now + timedelta(minutes=body.minutes)
        await session.commit()
        await session.refresh(r)
    return _serialize_reminder(r, tz)


@router.post("/reminders/{reminder_id}/stop")
async def reminder_stop(reminder_id: UUID, user_id: TmaUser) -> dict:
    ok = await stop_reminder_by_id(reminder_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}
