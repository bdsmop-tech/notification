import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from bot.config import MIN_SPAM_INTERVAL_SECONDS, READ_ACK_INTERVAL_SECONDS, REMINDER_POLL_SECONDS
from bot.database import SessionLocal
from bot.models import Reminder, UserSettings
from bot.quiet_hours import next_quiet_end_utc
from bot.user_prefs import get_user_zone

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _firing_keyboard(r: Reminder) -> InlineKeyboardMarkup | None:
    if r.spam_until_read:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Прочитал", callback_data=f"ack:{r.id}")]]
        )
    if r.spam_interval_seconds and r.spam_interval_seconds > 0:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Стоп", callback_data=f"stop:{r.id}")],
                [
                    InlineKeyboardButton("+5 мин", callback_data=f"snz:{r.id}:5"),
                    InlineKeyboardButton("+1 ч", callback_data=f"snz:{r.id}:60"),
                    InlineKeyboardButton("Завтра", callback_data=f"snz:{r.id}:1440"),
                ],
            ]
        )
    return None


async def _process_due_reminders(app: Application) -> None:
    now = _utcnow()
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.active.is_(True), Reminder.fire_at <= now)
        )
        due = result.scalars().all()
        if not due:
            return

        for r in due:
            prefs = await session.get(UserSettings, r.user_id)
            tz = await get_user_zone(r.user_id)
            if prefs and prefs.quiet_hours_enabled:
                nq = next_quiet_end_utc(
                    now,
                    tz,
                    prefs.quiet_start_hour,
                    prefs.quiet_end_hour,
                )
                if nq is not None:
                    await session.execute(
                        update(Reminder).where(Reminder.id == r.id).values(fire_at=nq)
                    )
                    continue

            text = r.text
            kb = _firing_keyboard(r)
            try:
                await app.bot.send_message(
                    chat_id=r.chat_id,
                    text=text,
                    reply_markup=kb,
                )
            except Exception as e:
                log.exception("send_message failed for reminder %s: %s", r.id, e)
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == r.id)
                    .values(active=False, closed_at=now)
                )
                continue

            repeating = bool(r.spam_interval_seconds and r.spam_interval_seconds > 0) or r.spam_until_read
            if repeating:
                if r.spam_until_read:
                    base = r.spam_interval_seconds or READ_ACK_INTERVAL_SECONDS
                    interval = max(base, READ_ACK_INTERVAL_SECONDS, MIN_SPAM_INTERVAL_SECONDS)
                else:
                    interval = max(r.spam_interval_seconds, MIN_SPAM_INTERVAL_SECONDS)
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == r.id)
                    .values(fire_at=now + timedelta(seconds=interval))
                )
            else:
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == r.id)
                    .values(active=False, closed_at=now)
                )

        await session.commit()


async def reminder_loop(app: Application) -> None:
    await asyncio.sleep(1)
    while True:
        try:
            await _process_due_reminders(app)
        except Exception:
            log.exception("reminder loop tick failed")
        await asyncio.sleep(REMINDER_POLL_SECONDS)


async def stop_reminder_by_id(reminder_id: UUID, user_id: int) -> bool:
    async with SessionLocal() as session:
        result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
        r = result.scalar_one_or_none()
        if r is None or r.user_id != user_id:
            return False
        await session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(active=False, closed_at=_utcnow())
        )
        await session.commit()
        return True
