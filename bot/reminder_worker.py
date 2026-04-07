import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from bot.config import MIN_SPAM_INTERVAL_SECONDS, REMINDER_POLL_SECONDS
from bot.database import SessionLocal
from bot.models import Reminder

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


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
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Стоп",
                            callback_data=f"stop:{r.id}",
                        )
                    ]
                ]
            )
            text = f"⏰ Напоминание:\n\n{r.text}"
            try:
                await app.bot.send_message(
                    chat_id=r.chat_id,
                    text=text,
                    reply_markup=keyboard,
                )
            except Exception as e:
                log.exception("send_message failed for reminder %s: %s", r.id, e)
                await session.execute(
                    update(Reminder)
                    .where(Reminder.id == r.id)
                    .values(active=False)
                )
                continue

            if r.spam_interval_seconds and r.spam_interval_seconds > 0:
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
                    .values(active=False)
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
            update(Reminder).where(Reminder.id == reminder_id).values(active=False)
        )
        await session.commit()
        return True
