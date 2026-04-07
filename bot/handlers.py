import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.config import DEFAULT_TZ, MIN_SPAM_INTERVAL_SECONDS
from bot.database import SessionLocal
from bot.models import Reminder
from bot.reminder_worker import stop_reminder_by_id

ASK_TEXT, ASK_WHEN, ASK_SPAM = range(3)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Привет! Я бот-напоминалка.\n\n"
        "/new — создать напоминание (дата и время, опционально «спам» каждые N секунд)\n"
        "/list — активные напоминания\n"
        "/cancel — отменить текущий диалог\n\n"
        f"Часовой пояс по умолчанию: {DEFAULT_TZ.key}\n"
        "Формат времени: ДД.ММ.ГГГГ ЧЧ:ММ (например 07.04.2026 14:30)"
    )


async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    await update.message.reply_text("Напиши текст напоминания одним сообщением.")
    return ASK_TEXT


async def new_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return ASK_TEXT
    context.user_data["reminder_text"] = update.message.text.strip()
    await update.message.reply_text(
        "Укажи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
        f"(интерпретирую как {DEFAULT_TZ.key})"
    )
    return ASK_WHEN


async def new_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        if update.message:
            await update.message.reply_text("Нужна дата и время. Пример: 07.04.2026 14:30")
        return ASK_WHEN
    raw = update.message.text.strip()
    try:
        naive = datetime.strptime(raw, "%d.%m.%Y %H:%M")
    except ValueError:
        await update.message.reply_text("Не понял формат. Нужно: ДД.ММ.ГГГГ ЧЧ:ММ")
        return ASK_WHEN
    local = naive.replace(tzinfo=DEFAULT_TZ)
    fire_at = local.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await update.message.reply_text("Это время уже прошло. Укажи момент в будущем.")
        return ASK_WHEN
    context.user_data["fire_at"] = fire_at
    await update.message.reply_text(
        "Как часто повторять напоминание (спам), в секундах?\n"
        "0 — один раз.\n"
        f"Если больше 0, минимальный интервал {MIN_SPAM_INTERVAL_SECONDS} сек."
    )
    return ASK_SPAM


async def new_spam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or update.message.text is None:
        return ASK_SPAM
    raw = update.message.text.strip()
    if not re.fullmatch(r"\d+", raw):
        await update.message.reply_text("Нужно целое число секунд, например 0 или 60")
        return ASK_SPAM
    spam = int(raw)
    if spam < 0:
        await update.message.reply_text("Число не может быть отрицательным.")
        return ASK_SPAM
    if spam > 0 and spam < MIN_SPAM_INTERVAL_SECONDS:
        spam = MIN_SPAM_INTERVAL_SECONDS
    text = context.user_data.get("reminder_text")
    fire_at = context.user_data.get("fire_at")
    if not text or not isinstance(fire_at, datetime):
        await update.message.reply_text("Что-то пошло не так. Начни снова: /new")
        return ConversationHandler.END
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return ConversationHandler.END
    async with SessionLocal() as session:
        r = Reminder(
            user_id=user.id,
            chat_id=chat.id,
            text=text,
            fire_at=fire_at,
            spam_interval_seconds=spam,
            active=True,
        )
        session.add(r)
        await session.commit()
        rid = r.id
    context.user_data.clear()
    await update.message.reply_text(
        f"Готово. Напоминание #{str(rid)[:8]}… запланировано на "
        f"{fire_at.astimezone(DEFAULT_TZ).strftime('%d.%m.%Y %H:%M')} ({DEFAULT_TZ.key})."
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Ок, отменил.")
    context.user_data.clear()
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    uid = update.effective_user.id
    async with SessionLocal() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == uid, Reminder.active.is_(True))
            .order_by(Reminder.fire_at.asc())
        )
        rows = result.scalars().all()
    if not rows:
        await update.message.reply_text("Активных напоминаний нет.")
        return
    lines = []
    for r in rows:
        local = r.fire_at.astimezone(DEFAULT_TZ)
        spam = f", спам каждые {r.spam_interval_seconds}s" if r.spam_interval_seconds else ", один раз"
        lines.append(f"• #{str(r.id)[:8]}… — {local.strftime('%d.%m.%Y %H:%M')}{spam}\n  {r.text[:80]}")
    await update.message.reply_text("\n\n".join(lines))


async def on_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"stop:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    try:
        rid = UUID(m.group(1))
    except ValueError:
        return
    ok = await stop_reminder_by_id(rid, update.effective_user.id)
    if ok:
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=q.message.chat_id, text="Напоминание остановлено.")
    else:
        await q.edit_message_reply_markup(reply_markup=None)


def register_handlers(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_start)],
        states={
            ASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_text)],
            ASK_WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_when)],
            ASK_SPAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_spam)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(on_stop_callback, pattern=r"^stop:"))
