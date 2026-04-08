"""REST API Mini App: тот же функционал, что у бота (напоминания, пояс, тихие часы)."""

from __future__ import annotations

import calendar as cal_module
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, nulls_last, select, update

from bot.config import BOT_TOKEN, MIN_SPAM_INTERVAL_SECONDS, READ_ACK_INTERVAL_SECONDS
from bot.database import SessionLocal
from bot.friends_service import (
    create_friend_request,
    is_friend,
    list_friends,
    list_incoming_requests,
    remove_friend,
    resolve_profile_name,
    respond_friend_request,
    user_id_by_profile_name,
)
from bot.models import FriendReminder, Reminder
from bot.time_parse import parse_time_one_line, parse_trailing_text_and_time
from bot.tma_validate import validate_telegram_init_data
from bot.user_prefs import (
    format_tz_label,
    get_user_profile_name,
    get_user_settings_row,
    get_user_zone,
    set_user_profile_name,
    set_user_timezone_offset_hours,
    toggle_quiet_hours,
)
from bot.web_auth import (
    exchange_code_for_session,
    revoke_session,
    user_id_from_login_code,
    user_id_from_session,
)

PAGE_SIZE = 5

router = APIRouter(prefix="/api", tags=["miniapp"])


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _forwarded_https(request: Request) -> bool:
    """За reverse-proxy HTTPS часто только в X-Forwarded-Proto — иначе Secure-cookie не ставится."""
    if request.url.scheme == "https":
        return True
    xf = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return xf == "https"


async def require_user(
    request: Request,
    authorization: str | None = Header(None),
    x_user_code: str | None = Header(None, alias="X-User-Code"),
) -> int:
    # Telegram Mini App
    if authorization and authorization.startswith("tma "):
        raw = authorization[4:].strip()
        data = validate_telegram_init_data(raw, BOT_TOKEN)
        if not data or "user" not in data:
            raise HTTPException(status_code=401, detail="Invalid initData")
        try:
            return int(data["user"]["id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Invalid user in initData") from None

    # Web/PWA: session token (основной способ — Bearer не режут прокси)
    if authorization:
        auth_stripped = authorization.strip()
        if auth_stripped.lower().startswith("bearer "):
            raw_tok = auth_stripped[7:].strip()
            if raw_tok:
                uid = await user_id_from_session(raw_tok)
                if uid:
                    return uid

    # Web/PWA: cookie (надёжнее, чем кастомные заголовки за прокси)
    cookie_code = request.cookies.get("user_code")
    if cookie_code and str(cookie_code).strip():
        uid = await user_id_from_login_code(str(cookie_code))
        if uid:
            return uid

    # Web/PWA: заголовки (fallback)
    if x_user_code and x_user_code.strip():
        uid = await user_id_from_login_code(x_user_code)
        if uid:
            return uid

    if authorization and authorization.lower().startswith("logincode "):
        uid = await user_id_from_login_code(authorization[10:].strip())
        if uid:
            return uid

    # Старые клиенты: cookie / заголовок sid (WebSession)
    sid = request.cookies.get("sid")
    if sid:
        uid = await user_id_from_session(sid)
        if uid:
            return uid

    if authorization and authorization.startswith("sid "):
        raw_sid = authorization[4:].strip()
        if raw_sid:
            uid = await user_id_from_session(raw_sid)
            if uid:
                return uid

    raise HTTPException(status_code=401, detail="Missing auth")


UserId = Annotated[int, Depends(require_user)]


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


def _serialize_friend_request(r) -> dict:
    return {
        "id": r.id,
        "from_user_id": r.from_user_id,
        "to_user_id": r.to_user_id,
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "responded_at": r.responded_at.isoformat() if r.responded_at else None,
    }


def _serialize_friend_reminder(x: FriendReminder) -> dict:
    return {
        "id": x.id,
        "sender_user_id": x.sender_user_id,
        "receiver_user_id": x.receiver_user_id,
        "reminder_id": str(x.reminder_id),
        "fire_at_sender_tz": x.fire_at_sender_tz,
        "status": x.status,
        "created_at": x.created_at.isoformat() if x.created_at else None,
        "delivered_at": x.delivered_at.isoformat() if x.delivered_at else None,
        "closed_at": x.closed_at.isoformat() if x.closed_at else None,
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
async def api_me(user_id: UserId) -> dict:
    tz = await get_user_zone(user_id)
    row = await get_user_settings_row(user_id)
    off = await _offset_hours(user_id)
    return {
        "user_id": user_id,
        "tz_label": format_tz_label(tz),
        "profile_name": (row.profile_name if row and row.profile_name else None),
        "offset_hours": off,
        "quiet_hours_enabled": bool(row and row.quiet_hours_enabled),
        "min_spam_interval_seconds": MIN_SPAM_INTERVAL_SECONDS,
        "read_ack_interval_seconds": READ_ACK_INTERVAL_SECONDS,
    }


class TimezoneBody(BaseModel):
    offset_hours: int = Field(ge=-12, le=14)


@router.post("/me/timezone")
async def api_set_timezone(body: TimezoneBody, user_id: UserId) -> dict:
    try:
        tz = await set_user_timezone_offset_hours(user_id, body.offset_hours)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid offset") from None
    return {"tz_label": format_tz_label(tz), "offset_hours": body.offset_hours}


class ProfileNameBody(BaseModel):
    profile_name: str = Field(min_length=1, max_length=64)


@router.post("/me/profile-name")
async def api_set_profile_name(body: ProfileNameBody, user_id: UserId) -> dict:
    try:
        name = await set_user_profile_name(user_id, body.profile_name)
    except ValueError as e:
        code = str(e)
        if code == "duplicate profile name":
            raise HTTPException(status_code=400, detail="Это имя профиля уже занято.") from None
        raise HTTPException(status_code=400, detail="invalid profile_name") from None
    return {"profile_name": name}


@router.post("/me/quiet-hours/toggle")
async def api_toggle_quiet(user_id: UserId) -> dict:
    on = await toggle_quiet_hours(user_id)
    return {"quiet_hours_enabled": on}


@router.get("/calendar/{year}/{month}")
async def api_calendar(year: int, month: int, user_id: UserId) -> dict:
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
async def reminders_active(user_id: UserId, page: int = 0) -> dict:
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
async def reminders_today(user_id: UserId) -> dict:
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
async def reminders_history(user_id: UserId, page: int = 0) -> dict:
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
async def reminder_one(reminder_id: UUID, user_id: UserId) -> dict:
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not found")
        fr = await session.scalar(
            select(FriendReminder).where(
                FriendReminder.reminder_id == reminder_id,
                FriendReminder.sender_user_id == user_id,
            )
        )
        if r.user_id != user_id and fr is None:
            raise HTTPException(status_code=404, detail="not found")
    tz = await get_user_zone(user_id)
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
async def reminder_create(body: CreateReminderBody, user_id: UserId) -> dict:
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
async def reminder_patch(reminder_id: UUID, body: PatchReminderBody, user_id: UserId) -> dict:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or not r.active:
            raise HTTPException(status_code=404, detail="not found")
        fr = await session.scalar(
            select(FriendReminder).where(
                FriendReminder.reminder_id == reminder_id,
                FriendReminder.sender_user_id == user_id,
            )
        )
        if r.user_id != user_id and fr is None:
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
            if fr is not None:
                fr.fire_at_sender_tz = local_dt.strftime("%d.%m.%Y %H:%M")
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
async def reminder_patch_spam(reminder_id: UUID, body: PatchSpamBody, user_id: UserId) -> dict:
    """Только режим повтора (как кнопки ens: в боте)."""
    tz = await get_user_zone(user_id)
    spam, until_read = _spam_variant_to_db(body.spam_variant, body.spam_interval_seconds)
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or not r.active:
            raise HTTPException(status_code=404, detail="not found")
        fr = await session.scalar(
            select(FriendReminder).where(
                FriendReminder.reminder_id == reminder_id,
                FriendReminder.sender_user_id == user_id,
            )
        )
        if r.user_id != user_id and fr is None:
            raise HTTPException(status_code=404, detail="not found")
        r.spam_interval_seconds = spam
        r.spam_until_read = until_read
        await session.commit()
        await session.refresh(r)
    return _serialize_reminder(r, tz)


@router.post("/reminders/{reminder_id}/archive")
async def reminder_archive(reminder_id: UUID, user_id: UserId) -> dict:
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not found")
        fr = await session.scalar(
            select(FriendReminder).where(FriendReminder.reminder_id == reminder_id)
        )
        is_owner = r.user_id == user_id
        is_sender = fr is not None and fr.sender_user_id == user_id
        if not is_owner and not is_sender:
            raise HTTPException(status_code=404, detail="not found")
        await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(active=False, closed_at=now)
        )
        if fr is not None:
            await session.execute(
                update(FriendReminder)
                .where(FriendReminder.reminder_id == reminder_id)
                .values(status="closed", closed_at=now)
            )
        await session.commit()
    return {"ok": True}


class SnoozeBody(BaseModel):
    minutes: int = Field(ge=1, le=10080)


@router.post("/reminders/{reminder_id}/snooze")
async def reminder_snooze(reminder_id: UUID, body: SnoozeBody, user_id: UserId) -> dict:
    tz = await get_user_zone(user_id)
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None or not r.active:
            raise HTTPException(status_code=404, detail="not found")
        fr = await session.scalar(
            select(FriendReminder).where(
                FriendReminder.reminder_id == reminder_id,
                FriendReminder.sender_user_id == user_id,
            )
        )
        if r.user_id != user_id and fr is None:
            raise HTTPException(status_code=404, detail="not found")
        r.fire_at = now + timedelta(minutes=body.minutes)
        if fr is not None:
            local = r.fire_at.astimezone(tz)
            fr.fire_at_sender_tz = local.strftime("%d.%m.%Y %H:%M")
        await session.commit()
        await session.refresh(r)
    return _serialize_reminder(r, tz)


@router.post("/reminders/{reminder_id}/stop")
async def reminder_stop(reminder_id: UUID, user_id: UserId) -> dict:
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, reminder_id)
        if r is None:
            raise HTTPException(status_code=404, detail="not found")
        fr = await session.scalar(
            select(FriendReminder).where(FriendReminder.reminder_id == reminder_id)
        )
        is_owner = r.user_id == user_id
        is_sender = fr is not None and fr.sender_user_id == user_id
        if not is_owner and not is_sender:
            raise HTTPException(status_code=404, detail="not found")
        await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(active=False, closed_at=now)
        )
        if fr is not None:
            await session.execute(
                update(FriendReminder)
                .where(FriendReminder.reminder_id == reminder_id)
                .values(status="closed", closed_at=now)
            )
        await session.commit()
    return {"ok": True}


class CreateFriendRequestBody(BaseModel):
    profile_name: str = Field(min_length=2, max_length=64)


@router.get("/friends")
async def friends_list(user_id: UserId) -> dict:
    ids = await list_friends(user_id)
    out = []
    for x in ids:
        out.append({"user_id": x, "display_name": await resolve_profile_name(x)})
    return {"friends": out}


@router.delete("/friends/{friend_user_id}")
async def friends_delete(friend_user_id: int, user_id: UserId) -> dict:
    ok = await remove_friend(user_id, friend_user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="friendship not found")
    return {"ok": True}


@router.get("/friends/requests/incoming")
async def friends_requests_incoming(user_id: UserId) -> dict:
    rows = await list_incoming_requests(user_id)
    out = []
    for x in rows:
        item = _serialize_friend_request(x)
        item["from_display_name"] = await resolve_profile_name(x.from_user_id)
        out.append(item)
    return {"requests": out}


@router.post("/friends/requests")
async def friends_request_create(body: CreateFriendRequestBody, user_id: UserId) -> dict:
    target_user_id = await user_id_by_profile_name(body.profile_name)
    if target_user_id is None:
        raise HTTPException(status_code=404, detail="Пользователь с таким именем не найден.")
    try:
        req = await create_friend_request(user_id, target_user_id)
    except ValueError as e:
        code = str(e)
        if code == "cannot_add_self":
            raise HTTPException(status_code=400, detail="cannot add self") from None
        if code == "target_not_activated":
            raise HTTPException(status_code=404, detail="Пользователь ещё не активировал бота. Попросите его сначала зайти в бот.") from None
        if code == "already_friends":
            raise HTTPException(status_code=400, detail="already friends") from None
        raise HTTPException(status_code=400, detail=code) from None
    return {"request": _serialize_friend_request(req)}


@router.post("/friends/requests/{request_id}/accept")
async def friends_request_accept(request_id: int, user_id: UserId) -> dict:
    try:
        req = await respond_friend_request(request_id, user_id, True)
    except ValueError:
        raise HTTPException(status_code=404, detail="request not found") from None
    return {"request": _serialize_friend_request(req)}


@router.post("/friends/requests/{request_id}/reject")
async def friends_request_reject(request_id: int, user_id: UserId) -> dict:
    try:
        req = await respond_friend_request(request_id, user_id, False)
    except ValueError:
        raise HTTPException(status_code=404, detail="request not found") from None
    return {"request": _serialize_friend_request(req)}


class CreateFriendReminderBody(BaseModel):
    text: str
    date: str
    time: str
    spam_variant: SpamVariant = "once"
    spam_interval_seconds: int = 0


@router.post("/friends/{friend_user_id}/reminders")
async def create_friend_reminder(friend_user_id: int, body: CreateFriendReminderBody, user_id: UserId) -> dict:
    if not await is_friend(user_id, friend_user_id):
        raise HTTPException(status_code=403, detail="not friends")
    tz_sender = await get_user_zone(user_id)
    d = _parse_local_date(body.date)
    t = parse_time_one_line(body.time)
    if t is None:
        raise HTTPException(status_code=400, detail="bad time, use 16 43 or 16:43")
    local_dt = datetime.combine(d, t, tzinfo=tz_sender)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        raise HTTPException(status_code=400, detail="время должно быть в будущем")
    spam, until_read = _spam_variant_to_db(body.spam_variant, body.spam_interval_seconds)

    async with SessionLocal() as session:
        rem = Reminder(
            user_id=friend_user_id,
            chat_id=friend_user_id,
            text=body.text.strip(),
            fire_at=fire_at,
            spam_interval_seconds=spam,
            spam_until_read=until_read,
            active=True,
        )
        session.add(rem)
        await session.flush()
        fr = FriendReminder(
            sender_user_id=user_id,
            receiver_user_id=friend_user_id,
            reminder_id=rem.id,
            fire_at_sender_tz=local_dt.strftime("%d.%m.%Y %H:%M"),
            status="scheduled",
        )
        session.add(fr)
        await session.commit()
        await session.refresh(rem)
        await session.refresh(fr)
    tz_receiver = await get_user_zone(friend_user_id)
    return {"reminder": _serialize_reminder(rem, tz_receiver), "outbox_item": _serialize_friend_reminder(fr)}


@router.get("/friends/reminders/outbox")
async def friend_reminders_outbox(user_id: UserId, page: int = 0) -> dict:
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(FriendReminder).where(FriendReminder.sender_user_id == user_id)
        )
        total = int(count or 0)
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(max(page, 0), pages - 1)
        result = await session.execute(
            select(FriendReminder, Reminder)
            .join(Reminder, Reminder.id == FriendReminder.reminder_id)
            .where(FriendReminder.sender_user_id == user_id)
            .order_by(FriendReminder.created_at.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        pairs = result.all()
    out = []
    for fr, rem in pairs:
        item = _serialize_friend_reminder(fr)
        item["receiver_display_name"] = await resolve_profile_name(fr.receiver_user_id)
        item["text"] = rem.text
        item["reminder_active"] = rem.active
        out.append(item)
    return {"items": out, "page": page, "pages": pages, "total": total}


class WebLoginBody(BaseModel):
    code: str


@router.post("/web/login")
async def web_login(body: WebLoginBody, request: Request, response: Response) -> dict:
    """Код из бота → долгоживущая сессия WebSession (token в JSON + cookie sid)."""
    session_days = 3650
    ex = await exchange_code_for_session(body.code, session_days=session_days)
    if not ex:
        raise HTTPException(status_code=401, detail="bad code")
    raw_token, _uid = ex
    max_age = session_days * 24 * 60 * 60
    secure_cookie = _forwarded_https(request)
    expires_at = _utcnow() + timedelta(seconds=max_age)
    response.delete_cookie("user_code", path="/")
    response.set_cookie(
        "sid",
        raw_token,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        max_age=max_age,
        expires=expires_at,
        path="/",
    )
    return {"ok": True, "token": raw_token, "expires_at": expires_at.isoformat()}


@router.post("/web/logout")
async def web_logout(request: Request, response: Response, authorization: str | None = Header(None)) -> dict:
    sid = request.cookies.get("sid")
    if sid:
        await revoke_session(sid)
    if authorization and authorization.strip().lower().startswith("bearer "):
        await revoke_session(authorization.strip()[7:].strip())
    response.delete_cookie("sid", path="/")
    response.delete_cookie("user_code", path="/")
    return {"ok": True}
