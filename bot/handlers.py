import re
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import nulls_last, select, update
from sqlalchemy.sql import func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.ext.filters import MessageFilter

from bot.calendar_kb import (
    build_calendar_keyboard,
    date_from_ymd_int,
    default_calendar_anchor,
    month_from_nav,
    parse_calendar_callback,
)
from bot.config import MIN_SPAM_INTERVAL_SECONDS, READ_ACK_INTERVAL_SECONDS
from bot.database import SessionLocal
from bot.keyboards import (
    back_to_menu_row,
    edit_spam_keyboard,
    main_menu_keyboard,
    settings_keyboard,
    spam_mode_keyboard,
    time_chips_keyboard,
)
from bot.models import Reminder
from bot.reminder_worker import stop_reminder_by_id
from bot.time_parse import parse_time_one_line
from bot.user_prefs import get_user_settings_row, get_user_zone, set_user_timezone, toggle_quiet_hours

ASK_TEXT, ASK_DATE, ASK_TIME, ASK_SPAM, ASK_SPAM_CUSTOM = range(5)
PAGE_SIZE = 5

_PENDING_EDIT_USER_IDS: set[int] = set()


def _mark_pending(uid: int) -> None:
    _PENDING_EDIT_USER_IDS.add(uid)


def _clear_pending(uid: int) -> None:
    _PENDING_EDIT_USER_IDS.discard(uid)


class _PendingEditFilter(MessageFilter):
    __slots__ = ()
    name = "PendingEdit"

    def filter(self, message: Message) -> bool:
        u = message.from_user
        return bool(u and u.id in _PENDING_EDIT_USER_IDS)


PENDING_EDIT_FILTER = _PendingEditFilter()

TZ_CHOICES = (
    ("Москва", "Europe/Moscow"),
    ("Калининград", "Europe/Kaliningrad"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("UTC", "UTC"),
    ("Лондон", "Europe/London"),
    ("Нью-Йорк", "America/New_York"),
    ("Токио", "Asia/Tokyo"),
)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _tz_picker_caption() -> str:
    lines = ["Часовой пояс — нажми цифру:\n"]
    for i, (label, _) in enumerate(TZ_CHOICES, start=1):
        lines.append(f"{i} — {label}")
    return "\n".join(lines)


def _tz_picker_markup() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(len(TZ_CHOICES)):
        row.append(InlineKeyboardButton(str(i + 1), callback_data=f"tzn:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(back_to_menu_row())
    return InlineKeyboardMarkup(rows)


def _new_calendar_kb(y: int, m: int) -> InlineKeyboardMarkup:
    cal = build_calendar_keyboard(y, m, "nd")
    rows = list(cal.inline_keyboard) + [
        [InlineKeyboardButton("« Отмена", callback_data="menu:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def _spam_label(r: Reminder) -> str:
    if r.spam_until_read:
        return f", до «Прочитал» каждые {READ_ACK_INTERVAL_SECONDS}s"
    if r.spam_interval_seconds:
        return f", повтор каждые {r.spam_interval_seconds}s"
    return ", один раз"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    tz = await get_user_zone(update.effective_user.id) if update.effective_user else None
    tz_line = f"Пояс: {tz.key}" if tz else ""
    await update.message.reply_text(
        "Напоминалка: всё ниже — через кнопки.\n" + tz_line,
        reply_markup=main_menu_keyboard(),
    )


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(_tz_picker_caption(), reply_markup=_tz_picker_markup())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Ок.", reply_markup=main_menu_keyboard())
    if update.effective_user:
        _clear_pending(update.effective_user.id)
    context.user_data.clear()
    return ConversationHandler.END


async def conv_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q:
        await q.answer("Отменено")
        await q.edit_message_text("Ок.", reply_markup=InlineKeyboardMarkup([back_to_menu_row()]))
    if update.effective_user:
        _clear_pending(update.effective_user.id)
    context.user_data.clear()
    return ConversationHandler.END


async def conv_menu_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выйти из /new по кнопкам меню без зависшего состояния."""
    for k in ("reminder_text", "picked_date", "fire_at", "spam_int", "spam_until_read"):
        context.user_data.pop(k, None)
    await on_menu_callback(update, context)
    return ConversationHandler.END


async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cq = update.callback_query
    if cq:
        await cq.answer()
        if cq.message:
            await cq.message.reply_text(
                "Напиши текст напоминания одним сообщением.",
            )
        if update.effective_user:
            _clear_pending(update.effective_user.id)
        context.user_data.clear()
        return ASK_TEXT
    if update.message is None:
        return ConversationHandler.END
    if update.effective_user:
        _clear_pending(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text("Напиши текст напоминания одним сообщением.")
    return ASK_TEXT


async def new_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return ASK_TEXT
    context.user_data["reminder_text"] = update.message.text.strip()
    y, m = default_calendar_anchor()
    await update.message.reply_text(
        "Выбери дату:",
        reply_markup=_new_calendar_kb(y, m),
    )
    return ASK_DATE


async def conv_new_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q is None or q.data is None:
        return ASK_DATE
    await q.answer()
    data = q.data
    if data == "noop":
        return ASK_DATE
    if not data.startswith("nd"):
        return ASK_DATE
    uid = update.effective_user.id if update.effective_user else 0
    tz = await get_user_zone(uid)
    try:
        action, payload = parse_calendar_callback(data, "nd")
    except ValueError:
        return ASK_DATE
    if action in ("p", "n") and payload is not None:
        dir_char = "n" if action == "n" else "p"
        ny, nm = month_from_nav(payload, dir_char)
        await q.edit_message_reply_markup(reply_markup=_new_calendar_kb(ny, nm))
        return ASK_DATE
    if action == "d" and payload is not None:
        picked = date_from_ymd_int(payload)
        context.user_data["picked_date"] = picked
        await q.edit_message_text(
            f"Дата: {picked.strftime('%d.%m.%Y')} ({tz.key}).\n"
            "Отправь время через пробел, например: 16 43 — или выбери быстрый вариант:",
            reply_markup=time_chips_keyboard(),
        )
        return ASK_TIME
    return ASK_DATE


async def conv_time_chip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return ASK_TIME
    await q.answer()
    m = re.fullmatch(r"nt:(\d{4}|manual)", q.data)
    if not m:
        return ASK_TIME
    tok = m.group(1)
    uid = update.effective_user.id
    tz = await get_user_zone(uid)
    picked = context.user_data.get("picked_date")
    if not isinstance(picked, date):
        await q.edit_message_text("Сначала выбери дату. /new")
        return ConversationHandler.END
    if tok == "manual":
        await q.edit_message_text(
            f"Дата {picked.strftime('%d.%m.%Y')} ({tz.key}). Отправь время через пробел, например: 16 43",
            reply_markup=None,
        )
        return ASK_TIME
    hh = int(tok[:2])
    mm = int(tok[2:])
    t = time(hh, mm)
    local_dt = datetime.combine(picked, t, tzinfo=tz)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await q.edit_message_text("Это время уже в прошлом.")
        return ConversationHandler.END
    context.user_data["fire_at"] = fire_at
    await q.edit_message_text("Как повторять напоминание?", reply_markup=spam_mode_keyboard())
    return ASK_SPAM


async def new_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return ASK_TIME
    uid = update.effective_user.id if update.effective_user else 0
    tz = await get_user_zone(uid)
    picked = context.user_data.get("picked_date")
    if not isinstance(picked, date):
        await update.message.reply_text("Начни снова: /new", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    t = parse_time_one_line(update.message.text)
    if t is None:
        await update.message.reply_text(
            "Нужно время: через пробел (16 43) или с двоеточием (16:43).",
            reply_markup=time_chips_keyboard(),
        )
        return ASK_TIME
    local_dt = datetime.combine(picked, t, tzinfo=tz)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await update.message.reply_text("Уже в прошлом. /new", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    context.user_data["fire_at"] = fire_at
    await update.message.reply_text("Как повторять?", reply_markup=spam_mode_keyboard())
    return ASK_SPAM


async def conv_spam_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return ASK_SPAM
    await q.answer()
    data = q.data
    if data == "ns:custom":
        await q.edit_message_text(
            "Отправь одно число — интервал в секундах (минимум "
            f"{MIN_SPAM_INTERVAL_SECONDS}, 0 = один раз).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Отмена", callback_data="menu:cancel")]]),
        )
        return ASK_SPAM_CUSTOM

    spam = 0
    until_read = False
    if data == "ns:0":
        pass
    elif data == "ns:read30":
        until_read = True
        spam = READ_ACK_INTERVAL_SECONDS
    elif data == "ns:30":
        spam = 30
    elif data == "ns:60":
        spam = 60
    elif data == "ns:120":
        spam = 120
    else:
        return ASK_SPAM

    context.user_data["spam_int"] = spam
    context.user_data["spam_until_read"] = until_read
    await _commit_new_reminder(update, context)
    return ConversationHandler.END


async def conv_spam_custom_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return ASK_SPAM_CUSTOM
    raw = update.message.text.strip()
    if not re.fullmatch(r"\d+", raw):
        await update.message.reply_text("Нужно целое число секунд.")
        return ASK_SPAM_CUSTOM
    spam = int(raw)
    if spam < 0:
        await update.message.reply_text("Не отрицательное.")
        return ASK_SPAM_CUSTOM
    if spam > 0 and spam < MIN_SPAM_INTERVAL_SECONDS:
        spam = MIN_SPAM_INTERVAL_SECONDS
    context.user_data["spam_int"] = spam
    context.user_data["spam_until_read"] = False
    await _commit_new_reminder(update, context)
    return ConversationHandler.END


async def _commit_new_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = context.user_data.get("reminder_text")
    fire_at = context.user_data.get("fire_at")
    spam = int(context.user_data.get("spam_int", 0))
    until_read = bool(context.user_data.get("spam_until_read", False))
    if not text or not isinstance(fire_at, datetime):
        return
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    async with SessionLocal() as session:
        r = Reminder(
            user_id=user.id,
            chat_id=chat.id,
            text=text,
            fire_at=fire_at,
            spam_interval_seconds=spam,
            spam_until_read=until_read,
            active=True,
        )
        session.add(r)
        await session.commit()
        rid = r.id
    tz = await get_user_zone(user.id)
    context.user_data.clear()
    msg = update.callback_query.message if update.callback_query and update.callback_query.message else update.message
    if msg:
        await msg.reply_text(
            f"Готово #{str(rid)[:8]}… на {fire_at.astimezone(tz).strftime('%d.%m.%Y %H:%M')} ({tz.key}).",
            reply_markup=main_menu_keyboard(),
        )


def _reminder_short(r: Reminder) -> str:
    return str(r.id)[:8]


async def _send_active_list(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    page: int,
    *,
    message=None,
    query=None,
) -> None:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.active.is_(True),
            )
        )
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.active.is_(True))
            .order_by(Reminder.fire_at.asc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        rows = result.scalars().all()
    total = int(count or 0)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 0), pages - 1)

    if not rows and total == 0:
        text = "Активных напоминаний нет."
        kb = InlineKeyboardMarkup([back_to_menu_row()]) if not query else None
    else:
        lines = [
            f"Активные (стр. {page + 1}/{pages}). Нажми строку — всё редактирование, 🗑 — в архив."
        ]
        buttons: list[list[InlineKeyboardButton]] = []
        for r in rows:
            local = r.fire_at.astimezone(tz)
            tail = f"{_spam_label(r)}"
            base = f"{local.strftime('%d.%m %H:%M')}{tail} — {r.text}"
            btn_text = base if len(base) <= 58 else base[:55] + "…"
            buttons.append(
                [
                    InlineKeyboardButton(btn_text, callback_data=f"em:{r.id}"),
                    InlineKeyboardButton("🗑", callback_data=f"rm:{r.id}"),
                ]
            )
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("« Пред.", callback_data=f"lp:{page - 1}"))
        if page < pages - 1:
            nav_row.append(InlineKeyboardButton("След. »", callback_data=f"lp:{page + 1}"))
        if nav_row:
            buttons.append(nav_row)
        buttons.append(back_to_menu_row())
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(buttons)

    if query and query.message:
        await query.edit_message_text(text, reply_markup=kb)
    elif message:
        await message.reply_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def _send_today_list(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    *,
    message=None,
    query=None,
) -> None:
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
    if not rows:
        text = "На сегодня запланированных напоминаний нет."
        kb = InlineKeyboardMarkup([back_to_menu_row()])
    else:
        lines = ["Сегодня:\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        for i, r in enumerate(rows):
            local = r.fire_at.astimezone(tz)
            lines.append(f"{i + 1}. {local.strftime('%H:%M')}{_spam_label(r)}\n   {r.text[:80]}")
            buttons.append(
                [
                    InlineKeyboardButton("✏️", callback_data=f"em:{r.id}"),
                    InlineKeyboardButton("🗑", callback_data=f"rm:{r.id}"),
                ]
            )
        buttons.append(back_to_menu_row())
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(buttons)

    if query and query.message:
        await query.edit_message_text(text, reply_markup=kb)
    elif message:
        await message.reply_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def _send_history_page(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    page: int,
    *,
    message=None,
    query=None,
) -> None:
    tz = await get_user_zone(user_id)
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count()).select_from(Reminder).where(
                Reminder.user_id == user_id,
                Reminder.active.is_(False),
            )
        )
        result = await session.execute(
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.active.is_(False))
            .order_by(nulls_last(Reminder.closed_at.desc()), Reminder.fire_at.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        rows = result.scalars().all()
    total = int(count or 0)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 0), pages - 1)

    if not rows and total == 0:
        text = "История пуста."
        kb = InlineKeyboardMarkup([back_to_menu_row()])
    else:
        lines = [f"История (стр. {page + 1}/{pages}):\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        start = page * PAGE_SIZE
        for i, r in enumerate(rows):
            local = r.fire_at.astimezone(tz)
            end = r.closed_at.astimezone(tz) if r.closed_at else None
            end_s = f" → {end.strftime('%d.%m %H:%M')}" if end else ""
            lines.append(f"{start + i + 1}. {local.strftime('%d.%m %H:%M')}{end_s}\n   {r.text[:100]}")
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("« Пред.", callback_data=f"hp:{page - 1}"))
        if page < pages - 1:
            nav_row.append(InlineKeyboardButton("След. »", callback_data=f"hp:{page + 1}"))
        if nav_row:
            buttons.append(nav_row)
        buttons.append(back_to_menu_row())
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(buttons)

    if query and query.message:
        await query.edit_message_text(text, reply_markup=kb)
    elif message:
        await message.reply_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    uid = update.effective_user.id
    chat_id = q.message.chat_id
    data = q.data
    if data == "menu:main":
        await q.edit_message_text("Главное меню", reply_markup=main_menu_keyboard())
        return
    if data == "menu:list":
        await _send_active_list(context, chat_id, uid, 0, query=q)
        return
    if data == "menu:history":
        await _send_history_page(context, chat_id, uid, 0, query=q)
        return
    if data == "menu:today":
        await _send_today_list(context, chat_id, uid, query=q)
        return
    if data == "menu:tz":
        await q.edit_message_text(_tz_picker_caption(), reply_markup=_tz_picker_markup())
        return
    if data == "menu:help":
        await q.edit_message_text(
            "• Новое — текст, дата, время через пробел (например 16 43) или кнопки.\n"
            "• Повтор: один раз, до «Прочитал», или интервал + Стоп.\n"
            "• В уведомлении: Прочитал, Стоп, отложить (+5 мин / +1 ч / завтра).\n"
            "• Тихие часы в настройках — не будит ночью (переносит на утро).",
            reply_markup=InlineKeyboardMarkup([back_to_menu_row()]),
        )
        return
    if data == "menu:settings":
        row = await get_user_settings_row(uid)
        on = bool(row and row.quiet_hours_enabled)
        await q.edit_message_text(
            "Настройки:",
            reply_markup=settings_keyboard(on),
        )
        return


async def on_stq_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or update.effective_user is None:
        return
    await q.answer()
    on = await toggle_quiet_hours(update.effective_user.id)
    await q.edit_message_text(
        "Настройки:",
        reply_markup=settings_keyboard(on),
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    await _send_active_list(
        context,
        update.message.chat_id,
        update.effective_user.id,
        0,
        message=update.message,
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    await _send_history_page(
        context,
        update.message.chat_id,
        update.effective_user.id,
        0,
        message=update.message,
    )


async def on_ack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"ack:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    try:
        rid = UUID(m.group(1))
    except ValueError:
        return
    ok = await stop_reminder_by_id(rid, update.effective_user.id)
    if ok:
        await q.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=q.message.chat_id, text="Отмечено как прочитано.")
    else:
        await q.edit_message_reply_markup(reply_markup=None)


async def on_snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"snz:([0-9a-fA-F-]{36}):(\d+)", q.data)
    if not m:
        return
    try:
        rid = UUID(m.group(1))
    except ValueError:
        return
    minutes = int(m.group(2))
    uid = update.effective_user.id
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, rid)
        if r is None or r.user_id != uid or not r.active:
            await q.edit_message_reply_markup(reply_markup=None)
            return
        r.fire_at = now + timedelta(minutes=minutes)
        await session.commit()
    await q.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text=f"Следующее напоминание: +{minutes} мин от текущего момента.",
    )


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
        await context.bot.send_message(chat_id=q.message.chat_id, text="Остановлено.")
    else:
        await q.edit_message_reply_markup(reply_markup=None)


async def on_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q:
        await q.answer()


async def on_tzn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"tzn:(\d+)", q.data)
    if not m:
        return
    idx = int(m.group(1))
    if idx < 0 or idx >= len(TZ_CHOICES):
        return
    name = TZ_CHOICES[idx][1]
    try:
        z = await set_user_timezone(update.effective_user.id, name)
    except Exception:
        await q.edit_message_text("Не удалось установить пояс.")
        return
    await q.edit_message_text(f"Пояс: {z.key}", reply_markup=InlineKeyboardMarkup([back_to_menu_row()]))


async def on_list_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"lp:(\d+)", q.data)
    if not m:
        return
    page = int(m.group(1))
    await _send_active_list(
        context,
        q.message.chat_id,
        update.effective_user.id,
        page,
        query=q,
    )


async def on_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"hp:(\d+)", q.data)
    if not m:
        return
    page = int(m.group(1))
    await _send_history_page(
        context,
        q.message.chat_id,
        update.effective_user.id,
        page,
        query=q,
    )


async def _get_reminder_for_user(rid: UUID, user_id: int) -> Reminder | None:
    async with SessionLocal() as session:
        r = await session.get(Reminder, rid)
        if r is None or r.user_id != user_id:
            return None
        return r


async def on_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    uid = update.effective_user.id
    _clear_pending(uid)
    for k in (
        "waiting_edit_text",
        "waiting_edit_spam",
        "waiting_edit_time",
        "waiting_edit_date",
        "edit_reminder_id",
    ):
        context.user_data.pop(k, None)
    m = re.fullmatch(r"em:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    rid = UUID(m.group(1))
    r = await _get_reminder_for_user(rid, update.effective_user.id)
    if r is None or not r.active:
        await q.edit_message_text("Не найдено.")
        return
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Текст", callback_data=f"et:{r.id}")],
            [InlineKeyboardButton("Дата и время", callback_data=f"edt:{r.id}")],
            [InlineKeyboardButton("Повтор / «Прочитал»", callback_data=f"esm:{r.id}")],
            [InlineKeyboardButton("« К списку", callback_data="lp:0")],
        ]
    )
    await q.edit_message_text(
        f"#{_reminder_short(r)}… {r.fire_at.astimezone(await get_user_zone(uid)).strftime('%d.%m %H:%M')}"
        f"{_spam_label(r)}\n\n{r.text[:500]}",
        reply_markup=kb,
    )


async def on_edit_spam_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"esm:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    rid = UUID(m.group(1))
    r = await _get_reminder_for_user(rid, update.effective_user.id)
    if r is None or not r.active:
        await q.edit_message_text("Не найдено.")
        return
    await q.edit_message_text(
        "Выбери режим повтора:",
        reply_markup=edit_spam_keyboard(str(r.id)),
    )


async def on_edit_spam_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"ens:([0-9a-fA-F-]{36}):([a-z0-9]+)", q.data)
    if not m:
        return
    rid = UUID(m.group(1))
    code = m.group(2)
    uid = update.effective_user.id
    until_read = False
    spam = 0
    if code == "0":
        pass
    elif code == "r30":
        until_read = True
        spam = READ_ACK_INTERVAL_SECONDS
    elif code == "30":
        spam = 30
    elif code == "60":
        spam = 60
    else:
        return
    async with SessionLocal() as session:
        r = await session.get(Reminder, rid)
        if r is None or r.user_id != uid or not r.active:
            await q.edit_message_text("Не найдено.")
            return
        r.spam_interval_seconds = spam
        r.spam_until_read = until_read
        await session.commit()
    await q.edit_message_text("Сохранено.", reply_markup=InlineKeyboardMarkup([back_to_menu_row()]))


async def on_delete_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"rm:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    rid = UUID(m.group(1))
    uid = update.effective_user.id
    now = _utcnow()
    async with SessionLocal() as session:
        r = await session.get(Reminder, rid)
        if r is None or r.user_id != uid:
            await q.edit_message_text("Ошибка.")
            return
        await session.execute(
            update(Reminder)
            .where(Reminder.id == rid)
            .values(active=False, closed_at=now)
        )
        await session.commit()
    await q.edit_message_text("Удалено (в истории).", reply_markup=main_menu_keyboard())


async def on_edit_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    uid = update.effective_user.id
    m = re.fullmatch(r"et:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    rid = UUID(m.group(1))
    r = await _get_reminder_for_user(rid, uid)
    if r is None or not r.active:
        await context.bot.send_message(chat_id=q.message.chat_id, text="Не найдено.")
        return
    _clear_pending(uid)
    _mark_pending(uid)
    context.user_data["waiting_edit_text"] = str(rid)
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text="Пришли новый текст одним сообщением.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="edit:cancel")]]
        ),
    )


async def on_edit_datetime_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"edt:([0-9a-fA-F-]{36})", q.data)
    if not m:
        return
    rid = UUID(m.group(1))
    r = await _get_reminder_for_user(rid, update.effective_user.id)
    if r is None or not r.active:
        await context.bot.send_message(chat_id=q.message.chat_id, text="Не найдено.")
        return
    context.user_data["edit_reminder_id"] = str(rid)
    y, mo = default_calendar_anchor()
    kb = build_calendar_keyboard(y, mo, "ed")
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text="Новая дата:",
        reply_markup=kb,
    )


async def on_edit_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    data = q.data
    if data == "noop":
        return
    if not data.startswith("ed"):
        return
    rid_s = context.user_data.get("edit_reminder_id")
    if not rid_s:
        return
    try:
        rid = UUID(rid_s)
    except ValueError:
        return
    r = await _get_reminder_for_user(rid, update.effective_user.id)
    if r is None or not r.active:
        await q.edit_message_text("Не найдено.")
        return
    try:
        action, payload = parse_calendar_callback(data, "ed")
    except ValueError:
        return
    if action in ("p", "n") and payload is not None:
        ny, nm = month_from_nav(payload, "n" if action == "n" else "p")
        kb = build_calendar_keyboard(ny, nm, "ed")
        await q.edit_message_reply_markup(reply_markup=kb)
        return
    if action == "d" and payload is not None:
        picked = date_from_ymd_int(payload)
        uid = update.effective_user.id
        _clear_pending(uid)
        _mark_pending(uid)
        context.user_data["waiting_edit_time"] = rid_s
        context.user_data["waiting_edit_date"] = picked.isoformat()
        tz = await get_user_zone(uid)
        await q.edit_message_text(
            f"Дата {picked.strftime('%d.%m.%Y')} ({tz.key}).\n"
            "Время через пробел (например 16 43) или кнопка:",
            reply_markup=time_chips_keyboard(),
        )


async def on_pending_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text or update.effective_user is None:
        return
    uid = update.effective_user.id
    text_raw = update.message.text.strip()

    if tid := context.user_data.get("waiting_edit_text"):
        try:
            rid = UUID(tid)
        except ValueError:
            context.user_data.pop("waiting_edit_text", None)
            _clear_pending(uid)
            return
        async with SessionLocal() as session:
            r = await session.get(Reminder, rid)
            if r is None or r.user_id != uid or not r.active:
                await update.message.reply_text("Ошибка.")
                context.user_data.pop("waiting_edit_text", None)
                _clear_pending(uid)
                return
            r.text = text_raw
            await session.commit()
        context.user_data.pop("waiting_edit_text", None)
        _clear_pending(uid)
        await update.message.reply_text("Текст обновлён.", reply_markup=main_menu_keyboard())
        return

    if tid := context.user_data.get("waiting_edit_time"):
        try:
            rid = UUID(tid)
        except ValueError:
            context.user_data.pop("waiting_edit_time", None)
            context.user_data.pop("waiting_edit_date", None)
            _clear_pending(uid)
            return
        ds = context.user_data.get("waiting_edit_date")
        if not ds:
            return
        try:
            picked = date.fromisoformat(ds)
        except ValueError:
            return
        t = parse_time_one_line(text_raw)
        if t is None:
            await update.message.reply_text(
                "Нужно время: 16 43 или 16:43.",
                reply_markup=time_chips_keyboard(),
            )
            return
        tz = await get_user_zone(uid)
        local_dt = datetime.combine(picked, t, tzinfo=tz)
        fire_at = local_dt.astimezone(timezone.utc)
        if fire_at <= _utcnow():
            await update.message.reply_text("Уже в прошлом.")
            return
        async with SessionLocal() as session:
            r = await session.get(Reminder, rid)
            if r is None or r.user_id != uid or not r.active:
                context.user_data.pop("waiting_edit_time", None)
                context.user_data.pop("waiting_edit_date", None)
                context.user_data.pop("edit_reminder_id", None)
                _clear_pending(uid)
                return
            r.fire_at = fire_at
            await session.commit()
        context.user_data.pop("waiting_edit_time", None)
        context.user_data.pop("waiting_edit_date", None)
        context.user_data.pop("edit_reminder_id", None)
        _clear_pending(uid)
        await update.message.reply_text(
            f"Время: {fire_at.astimezone(tz).strftime('%d.%m.%Y %H:%M')}",
            reply_markup=main_menu_keyboard(),
        )


async def on_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or update.effective_user is None:
        return
    await q.answer()
    uid = update.effective_user.id
    _clear_pending(uid)
    context.user_data.pop("waiting_edit_text", None)
    await q.edit_message_text("Отменено.", reply_markup=main_menu_keyboard())


async def on_nt_standalone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Чипы времени nt: вне диалога /new — при редактировании даты напоминания (waiting_edit_time)."""
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    rid_s = context.user_data.get("waiting_edit_time")
    if not rid_s:
        return
    m = re.fullmatch(r"nt:(\d{4}|manual)", q.data)
    if not m:
        return
    tok = m.group(1)
    uid = update.effective_user.id
    tz = await get_user_zone(uid)
    ds = context.user_data.get("waiting_edit_date")
    if not ds:
        return
    try:
        picked = date.fromisoformat(ds)
        rid = UUID(rid_s)
    except ValueError:
        return
    r = await _get_reminder_for_user(rid, uid)
    if r is None or not r.active:
        await q.edit_message_text("Не найдено.")
        return
    if tok == "manual":
        await q.edit_message_text(
            f"Дата {picked.strftime('%d.%m.%Y')} ({tz.key}). Отправь время через пробел, например: 16 43",
            reply_markup=None,
        )
        return
    hh = int(tok[:2])
    mm = int(tok[2:])
    t = time(hh, mm)
    local_dt = datetime.combine(picked, t, tzinfo=tz)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await q.edit_message_text("Это время уже в прошлом.")
        context.user_data.pop("waiting_edit_time", None)
        context.user_data.pop("waiting_edit_date", None)
        _clear_pending(uid)
        return
    async with SessionLocal() as session:
        r2 = await session.get(Reminder, rid)
        if r2 is None or r2.user_id != uid or not r2.active:
            await q.edit_message_text("Не найдено.")
            return
        r2.fire_at = fire_at
        await session.commit()
    context.user_data.pop("waiting_edit_time", None)
    context.user_data.pop("waiting_edit_date", None)
    context.user_data.pop("edit_reminder_id", None)
    _clear_pending(uid)
    await q.edit_message_text(
        f"Время: {fire_at.astimezone(tz).strftime('%d.%m.%Y %H:%M')}",
        reply_markup=InlineKeyboardMarkup([back_to_menu_row()]),
    )


async def on_orphan_new_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сессия /new уже не активна, а callback от календаря пришёл — ответить, иначе крутится индикатор."""
    q = update.callback_query
    if q is None:
        return
    await q.answer()
    await q.edit_message_text(
        "Сессия создания напоминания сброшена. Открой меню и нажми «Новое».",
        reply_markup=main_menu_keyboard(),
    )


def register_handlers(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_start),
            CallbackQueryHandler(new_start, pattern=r"^menu:new$"),
        ],
        states={
            ASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_text)],
            ASK_DATE: [
                CallbackQueryHandler(conv_new_calendar, pattern=r"^(?:nd[pnd]:\d+|noop)$"),
            ],
            ASK_TIME: [
                CallbackQueryHandler(conv_time_chip, pattern=r"^nt:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_time),
            ],
            ASK_SPAM: [
                CallbackQueryHandler(conv_spam_select, pattern=r"^ns:"),
            ],
            ASK_SPAM_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_spam_custom_msg)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(conv_cancel_cb, pattern=r"^menu:cancel$"),
            CallbackQueryHandler(
                conv_menu_leave,
                pattern=r"^menu:(list|history|tz|help|main|today|settings)$",
            ),
        ],
    )
    # Важно: ConversationHandler раньше любого menu:* — иначе кнопки меню перехватываются до диалога,
    # а fallback разговора (conv_menu_leave) не срабатывает.
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_nt_standalone, pattern=r"^nt:"))
    app.add_handler(CallbackQueryHandler(on_orphan_new_calendar_callback, pattern=r"^nd[pnd]:"))
    app.add_handler(
        CallbackQueryHandler(
            on_menu_callback,
            pattern=r"^menu:(list|history|tz|help|main|today|settings)$",
        )
    )
    app.add_handler(CallbackQueryHandler(on_stq_toggle, pattern=r"^stq:toggle$"))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(CallbackQueryHandler(on_noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_tzn_callback, pattern=r"^tzn:\d+$"))
    app.add_handler(CallbackQueryHandler(on_list_page, pattern=r"^lp:\d+$"))
    app.add_handler(CallbackQueryHandler(on_history_page, pattern=r"^hp:\d+$"))
    app.add_handler(CallbackQueryHandler(on_edit_menu, pattern=r"^em:"))
    app.add_handler(CallbackQueryHandler(on_edit_spam_menu, pattern=r"^esm:"))
    app.add_handler(CallbackQueryHandler(on_edit_spam_apply, pattern=r"^ens:"))
    app.add_handler(CallbackQueryHandler(on_delete_reminder, pattern=r"^rm:"))
    app.add_handler(CallbackQueryHandler(on_edit_text_start, pattern=r"^et:"))
    app.add_handler(CallbackQueryHandler(on_edit_datetime_start, pattern=r"^edt:"))
    app.add_handler(CallbackQueryHandler(on_edit_calendar, pattern=r"^ed[pnd]:"))
    app.add_handler(CallbackQueryHandler(on_ack_callback, pattern=r"^ack:"))
    app.add_handler(CallbackQueryHandler(on_snooze_callback, pattern=r"^snz:"))
    app.add_handler(CallbackQueryHandler(on_stop_callback, pattern=r"^stop:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & PENDING_EDIT_FILTER,
            on_pending_edit_message,
        )
    )
    app.add_handler(CallbackQueryHandler(on_edit_cancel, pattern=r"^edit:cancel$"))
