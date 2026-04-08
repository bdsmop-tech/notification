import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import nulls_last, select, update as sql_update
from sqlalchemy.sql import func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
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
from bot.config import MIN_SPAM_INTERVAL_SECONDS, READ_ACK_INTERVAL_SECONDS, WEBAPP_PUBLIC_URL
from bot.database import SessionLocal
from bot.friends_service import (
    create_friend_request,
    list_friends,
    list_incoming_requests,
    resolve_profile_name,
    respond_friend_request,
    user_id_by_profile_name,
)
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
from bot.time_parse import parse_time_one_line, parse_trailing_text_and_time
from bot.user_prefs import (
    format_tz_label,
    get_user_profile_name,
    get_user_settings_row,
    get_user_zone,
    set_user_profile_name,
    set_user_timezone_offset_hours,
    touch_user_settings,
    toggle_quiet_hours,
)
from bot.web_auth import issue_login_code

ASK_TEXT, ASK_DATE, ASK_TIME, ASK_SPAM, ASK_SPAM_CUSTOM = range(5)
PAGE_SIZE = 5

log = logging.getLogger(__name__)

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

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _tz_picker_caption() -> str:
    return (
        "Часовой пояс: смещение от UTC в часах, например +3, -4 или 0.\n"
        "Допустимо от −12 до +14. Нажми кнопку или введи вручную (см. «Ввести»)."
    )


def _tz_offset_markup() -> InlineKeyboardMarkup:
    hours = list(range(-12, 15))
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for h in hours:
        label = f"{h:+d}" if h != 0 else "0"
        row.append(InlineKeyboardButton(label, callback_data=f"tzo:{h}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ Ввести (+3 или -4)", callback_data="tzo:manual")])
    rows.append(back_to_menu_row())
    return InlineKeyboardMarkup(rows)


def _parse_tz_offset_hours(text: str) -> int | None:
    s = text.strip()
    m = re.fullmatch(r"([+-]?)(\d{1,2})", s)
    if not m:
        return None
    sign, n = m.group(1), int(m.group(2))
    if sign == "-":
        h = -n
    elif sign == "+":
        h = n
    else:
        h = n
    if -12 <= h <= 14:
        return h
    return None


def _new_calendar_kb(y: int, m: int, *, history_back_page: int | None = None) -> InlineKeyboardMarkup:
    cal = build_calendar_keyboard(y, m, "nd")
    rows = list(cal.inline_keyboard)
    if history_back_page is not None:
        rows.append(
            [InlineKeyboardButton("« К истории", callback_data=f"hhist:{history_back_page}")],
        )
    rows.append([InlineKeyboardButton("« Отмена", callback_data="menu:cancel")])
    return InlineKeyboardMarkup(rows)


def _hist_page(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    return context.user_data.get("history_return_page")


def _kb_time_chips(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return time_chips_keyboard(history_back_page=_hist_page(context))


def _kb_spam(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return spam_mode_keyboard(history_back_page=_hist_page(context))


def _spam_custom_error_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    hp = _hist_page(context)
    rows: list[list[InlineKeyboardButton]] = []
    if hp is not None:
        rows.append([InlineKeyboardButton("« К истории", callback_data=f"hhist:{hp}")])
    rows.append([InlineKeyboardButton("« К режиму повтора", callback_data="ns:bspam")])
    rows.append([InlineKeyboardButton("« Отмена", callback_data="menu:cancel")])
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
    if update.effective_user:
        await touch_user_settings(update.effective_user.id)
    tz = await get_user_zone(update.effective_user.id) if update.effective_user else None
    tz_line = f"Пояс: {format_tz_label(tz)}" if tz else ""
    code_line = ""
    if update.effective_user:
        code = await issue_login_code(update.effective_user.id)
        web_link = (WEBAPP_PUBLIC_URL.rstrip("/") + "/web") if WEBAPP_PUBLIC_URL else "/web"
        profile = await get_user_profile_name(update.effective_user.id)
        if not profile:
            context.user_data["await_profile_name"] = True
            profile_tip = "\n\nСначала укажи имя профиля (как тебя будут видеть друзья). Отправь его одним сообщением."
        else:
            profile_tip = ""
        code_line = (
            f"\n\nКод для входа на сайт: {code}\n"
            f"Ссылка для входа: {web_link}\n"
            "Введи этот код на странице (постоянный)."
        ) + profile_tip
    await update.message.reply_text(
        "Напоминалка: кнопки или одна строка «текст 16 43» (на сегодня).\n" + tz_line + code_line,
        reply_markup=main_menu_keyboard(),
    )


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    args = context.args or []
    if len(args) == 1:
        h = _parse_tz_offset_hours(args[0])
        if h is not None:
            try:
                tz = await set_user_timezone_offset_hours(update.effective_user.id, h)
            except ValueError:
                await update.message.reply_text("Нужно число от −12 до +14.")
                return
            context.user_data.pop("await_tz_offset", None)
            await update.message.reply_text(
                f"Пояс: {format_tz_label(tz)}",
                reply_markup=main_menu_keyboard(),
            )
            return
    context.user_data["await_tz_offset"] = True
    await update.message.reply_text(_tz_picker_caption(), reply_markup=_tz_offset_markup())


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
    for k in ("reminder_text", "picked_date", "fire_at", "spam_int", "spam_until_read", "history_return_page"):
        context.user_data.pop(k, None)
    await on_menu_callback(update, context)
    return ConversationHandler.END


async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cq = update.callback_query
    if cq:
        await cq.answer()
        if cq.message:
            await cq.message.reply_text(
                "Напиши текст напоминания. Можно одной строкой: «текст 16 43» — на сегодня в это время.",
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
    await update.message.reply_text(
        "Напиши текст напоминания. Можно одной строкой: «текст 16 43» — на сегодня в это время.",
    )
    return ASK_TEXT


async def history_dup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Повтор напоминания из истории — тот же текст, дата и время заново."""
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return ConversationHandler.END
    await q.answer()
    m = re.fullmatch(r"histdup:([0-9a-fA-F-]{36}):(\d+)", q.data)
    if not m:
        return ConversationHandler.END
    rid = UUID(m.group(1))
    page = int(m.group(2))
    uid = update.effective_user.id
    r = await _get_history_reminder(rid, uid)
    if r is None:
        await q.edit_message_text("Не найдено в истории.")
        return ConversationHandler.END
    _clear_pending(uid)
    context.user_data.clear()
    context.user_data["reminder_text"] = r.text
    context.user_data["history_return_page"] = page
    y, mo = default_calendar_anchor()
    await q.edit_message_text(
        "Тот же текст — выбери новую дату и время:",
        reply_markup=_new_calendar_kb(y, mo, history_back_page=page),
    )
    return ASK_DATE


async def new_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text or update.effective_user is None:
        return ASK_TEXT
    raw = update.message.text.strip()
    one = parse_trailing_text_and_time(raw)
    if one:
        text_part, t = one
        uid = update.effective_user.id
        tz = await get_user_zone(uid)
        today = _utcnow().astimezone(tz).date()
        local_dt = datetime.combine(today, t, tzinfo=tz)
        fire_at = local_dt.astimezone(timezone.utc)
        if fire_at <= _utcnow():
            context.user_data["reminder_text"] = text_part
            context.user_data["picked_date"] = today
            await update.message.reply_text(
                "Это время сегодня уже прошло. Укажи время в будущем (например 16 43) "
                "или /new и выбери другую дату.",
                reply_markup=_kb_time_chips(context),
            )
            return ASK_TIME
        context.user_data["reminder_text"] = text_part
        context.user_data["fire_at"] = fire_at
        await update.message.reply_text("Как повторять?", reply_markup=_kb_spam(context))
        return ASK_SPAM
    context.user_data["reminder_text"] = raw
    y, m = default_calendar_anchor()
    await update.message.reply_text(
        "Выбери дату:",
        reply_markup=_new_calendar_kb(y, m, history_back_page=_hist_page(context)),
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
        await q.edit_message_reply_markup(
            reply_markup=_new_calendar_kb(ny, nm, history_back_page=_hist_page(context)),
        )
        return ASK_DATE
    if action == "d" and payload is not None:
        picked = date_from_ymd_int(payload)
        context.user_data["picked_date"] = picked
        await q.edit_message_text(
            f"Дата: {picked.strftime('%d.%m.%Y')} ({format_tz_label(tz)}).\n"
            "Отправь время через пробел, например: 16 43 — или выбери быстрый вариант:",
            reply_markup=_kb_time_chips(context),
        )
        return ASK_TIME
    return ASK_DATE


async def conv_time_back_to_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """« К дате» — вернуться к календарю (создание / повтор из истории)."""
    q = update.callback_query
    if q is None or update.effective_user is None:
        return ASK_TIME
    await q.answer()
    context.user_data.pop("picked_date", None)
    context.user_data.pop("fire_at", None)
    y, m = default_calendar_anchor()
    await q.edit_message_text(
        "Выбери дату:",
        reply_markup=_new_calendar_kb(y, m, history_back_page=_hist_page(context)),
    )
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
            f"Дата {picked.strftime('%d.%m.%Y')} ({format_tz_label(tz)}). Отправь время через пробел, например: 16 43",
            reply_markup=None,
        )
        return ASK_TIME
    hh = int(tok[:2])
    mm = int(tok[2:])
    t = time(hh, mm)
    local_dt = datetime.combine(picked, t, tzinfo=tz)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await q.edit_message_text(
            "Это время уже прошло. Выбери другое время в будущем или введи вручную (16 43).",
            reply_markup=_kb_time_chips(context),
        )
        return ASK_TIME
    context.user_data["fire_at"] = fire_at
    await q.edit_message_text("Как повторять напоминание?", reply_markup=_kb_spam(context))
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
            reply_markup=_kb_time_chips(context),
        )
        return ASK_TIME
    local_dt = datetime.combine(picked, t, tzinfo=tz)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await update.message.reply_text(
            "Это время уже прошло. Укажи время в будущем (например 16 43).",
            reply_markup=_kb_time_chips(context),
        )
        return ASK_TIME
    context.user_data["fire_at"] = fire_at
    await update.message.reply_text("Как повторять?", reply_markup=_kb_spam(context))
    return ASK_SPAM


async def conv_spam_back_to_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """« К времени» — с шага выбора повтора назад к времени."""
    q = update.callback_query
    if q is None or update.effective_user is None:
        return ASK_SPAM
    await q.answer()
    picked = context.user_data.get("picked_date")
    if not isinstance(picked, date):
        return ASK_SPAM
    context.user_data.pop("fire_at", None)
    tz = await get_user_zone(update.effective_user.id)
    await q.edit_message_text(
        f"Дата: {picked.strftime('%d.%m.%Y')} ({format_tz_label(tz)}).\n"
        "Отправь время через пробел, например: 16 43 — или выбери быстрый вариант:",
        reply_markup=_kb_time_chips(context),
    )
    return ASK_TIME


async def conv_spam_custom_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """« К режиму повтора» — с ввода секунд назад к выбору ns:."""
    q = update.callback_query
    if q is None or update.effective_user is None:
        return ASK_SPAM_CUSTOM
    await q.answer()
    await q.edit_message_text("Как повторять?", reply_markup=_kb_spam(context))
    return ASK_SPAM


async def conv_spam_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return ASK_SPAM
    await q.answer()
    data = q.data
    if data == "ns:bspam":
        return ASK_SPAM
    if data == "ns:custom":
        custom_rows: list[list[InlineKeyboardButton]] = []
        hp = _hist_page(context)
        if hp is not None:
            custom_rows.append([InlineKeyboardButton("« К истории", callback_data=f"hhist:{hp}")])
        custom_rows.append([InlineKeyboardButton("« К режиму повтора", callback_data="ns:bspam")])
        custom_rows.append([InlineKeyboardButton("« Отмена", callback_data="menu:cancel")])
        await q.edit_message_text(
            "Отправь одно число — интервал в секундах (минимум "
            f"{MIN_SPAM_INTERVAL_SECONDS}, 0 = один раз).",
            reply_markup=InlineKeyboardMarkup(custom_rows),
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
        await update.message.reply_text(
            "Нужно целое число секунд.",
            reply_markup=_spam_custom_error_kb(context),
        )
        return ASK_SPAM_CUSTOM
    spam = int(raw)
    if spam < 0:
        await update.message.reply_text(
            "Не отрицательное.",
            reply_markup=_spam_custom_error_kb(context),
        )
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
            f"Готово #{str(rid)[:8]}… на {fire_at.astimezone(tz).strftime('%d.%m.%Y %H:%M')} ({format_tz_label(tz)}).",
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
        lines = [
            f"История (стр. {page + 1}/{pages}). Нажми — то же напоминание, время выберешь заново:",
        ]
        buttons: list[list[InlineKeyboardButton]] = []
        start = page * PAGE_SIZE
        for i, r in enumerate(rows):
            local = r.fire_at.astimezone(tz)
            end = r.closed_at.astimezone(tz) if r.closed_at else None
            end_s = f" → {end.strftime('%d.%m %H:%M')}" if end else ""
            lines.append(f"{start + i + 1}. {local.strftime('%d.%m %H:%M')}{end_s}\n   {r.text[:100]}")
            label = f"{local.strftime('%d.%m %H:%M')}{end_s} — {r.text}"
            if len(label) > 58:
                label = label[:55] + "…"
            buttons.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=f"histdup:{r.id}:{page}",
                    ),
                ]
            )
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


async def conv_hhist_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return ConversationHandler.END
    await q.answer()
    m = re.fullmatch(r"hhist:(\d+)", q.data)
    if not m:
        return ConversationHandler.END
    page = int(m.group(1))
    for k in (
        "reminder_text",
        "picked_date",
        "fire_at",
        "spam_int",
        "spam_until_read",
        "history_return_page",
    ):
        context.user_data.pop(k, None)
    await _send_history_page(
        context,
        q.message.chat_id,
        update.effective_user.id,
        page,
        query=q,
    )
    return ConversationHandler.END


async def on_hhist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"hhist:(\d+)", q.data)
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


def _friends_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить по TG ID", callback_data="fr:add")],
            [InlineKeyboardButton("📥 Входящие заявки", callback_data="fr:req")],
            [InlineKeyboardButton("📋 Мои друзья", callback_data="fr:list")],
            [InlineKeyboardButton("« Назад", callback_data="menu:main")],
        ]
    )


async def _friends_menu_text(uid: int) -> str:
    friends = await list_friends(uid)
    reqs = await list_incoming_requests(uid)
    return (
        "Друзья\n\n"
        f"• Подтверждённых друзей: {len(friends)}\n"
        f"• Входящих заявок: {len(reqs)}\n\n"
        "Добавляй друга по имени профиля."
    )


async def on_friends_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    uid = update.effective_user.id
    data = q.data
    if data == "fr:add":
        context.user_data["await_friend_name"] = True
        await q.edit_message_text(
            "Отправь имя профиля друга одним сообщением.",
            reply_markup=InlineKeyboardMarkup([back_to_menu_row()]),
        )
        return
    if data == "fr:list":
        ids = await list_friends(uid)
        if not ids:
            txt = "Пока нет друзей."
        else:
            names = []
            for x in ids[:50]:
                names.append("• " + await resolve_profile_name(x))
            txt = "Мои друзья:\n" + "\n".join(names)
        await q.edit_message_text(txt, reply_markup=_friends_menu_kb())
        return
    if data == "fr:req":
        reqs = await list_incoming_requests(uid)
        if not reqs:
            await q.edit_message_text("Входящих заявок нет.", reply_markup=_friends_menu_kb())
            return
        rows: list[list[InlineKeyboardButton]] = []
        for r in reqs[:20]:
            from_name = await resolve_profile_name(r.from_user_id)
            rows.append(
                [
                    InlineKeyboardButton(f"Принять {from_name}", callback_data=f"fr:acc:{r.id}"),
                    InlineKeyboardButton("Отклонить", callback_data=f"fr:rej:{r.id}"),
                ]
            )
        rows.append([InlineKeyboardButton("« Назад", callback_data="menu:friends")])
        await q.edit_message_text("Входящие заявки:", reply_markup=InlineKeyboardMarkup(rows))
        return
    m_acc = re.fullmatch(r"fr:acc:(\d+)", data)
    if m_acc:
        rid = int(m_acc.group(1))
        try:
            req = await respond_friend_request(rid, uid, True)
        except ValueError:
            await q.edit_message_text("Заявка не найдена.", reply_markup=_friends_menu_kb())
            return
        await q.edit_message_text("Заявка принята.", reply_markup=_friends_menu_kb())
        try:
            to_name = await resolve_profile_name(uid)
            await context.bot.send_message(
                chat_id=req.from_user_id,
                text=f"{to_name} принял(а) вашу заявку в друзья.",
            )
        except Exception as e:
            log.warning("notify requester accepted failed: %s", e)
        return
    m_rej = re.fullmatch(r"fr:rej:(\d+)", data)
    if m_rej:
        rid = int(m_rej.group(1))
        try:
            req = await respond_friend_request(rid, uid, False)
        except ValueError:
            await q.edit_message_text("Заявка не найдена.", reply_markup=_friends_menu_kb())
            return
        await q.edit_message_text("Заявка отклонена.", reply_markup=_friends_menu_kb())
        try:
            to_name = await resolve_profile_name(uid)
            await context.bot.send_message(
                chat_id=req.from_user_id,
                text=f"{to_name} отклонил(а) вашу заявку в друзья.",
            )
        except Exception as e:
            log.warning("notify requester rejected failed: %s", e)
        return


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    uid = update.effective_user.id
    chat_id = q.message.chat_id
    data = q.data
    if data != "menu:tz":
        context.user_data.pop("await_tz_offset", None)
    if data != "menu:friends":
        context.user_data.pop("await_friend_name", None)
    if data != "menu:settings":
        context.user_data.pop("await_profile_name", None)
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
        context.user_data["await_tz_offset"] = True
        await q.edit_message_text(_tz_picker_caption(), reply_markup=_tz_offset_markup())
        return
    if data == "menu:help":
        await q.edit_message_text(
            "• Новое — текст и календарь; или одной строкой «текст 16 43» на сегодня.\n"
            "• Пояс UTC: +3, −4 или кнопки.\n"
            "• Повтор: один раз, до «Прочитал», или интервал + Стоп.\n"
            "• В уведомлении: Прочитал, Стоп, отложить (+5 мин / +1 ч / завтра).\n"
            "• История — кнопка по строке: тот же текст, выбираешь дату и время заново.\n"
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
    if data == "menu:friends":
        await q.edit_message_text(
            await _friends_menu_text(uid),
            reply_markup=_friends_menu_kb(),
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


async def on_tzo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or q.data is None or update.effective_user is None:
        return
    await q.answer()
    m = re.fullmatch(r"tzo:(.+)", q.data)
    if not m:
        return
    tok = m.group(1)
    uid = update.effective_user.id
    if tok == "manual":
        context.user_data["await_tz_offset"] = True
        await q.edit_message_text(
            "Отправь смещение от UTC одним сообщением, например +3 или -4 (от −12 до +14).",
            reply_markup=InlineKeyboardMarkup([back_to_menu_row()]),
        )
        return
    try:
        h = int(tok)
    except ValueError:
        return
    if h < -12 or h > 14:
        await q.edit_message_text("Нужно число от −12 до +14.")
        return
    try:
        tz = await set_user_timezone_offset_hours(uid, h)
    except ValueError:
        await q.edit_message_text("Не удалось сохранить.")
        return
    context.user_data.pop("await_tz_offset", None)
    await q.edit_message_text(
        f"Пояс: {format_tz_label(tz)}",
        reply_markup=InlineKeyboardMarkup([back_to_menu_row()]),
    )


async def on_tz_offset_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("await_profile_name"):
        if update.message is None or not update.message.text or update.effective_user is None:
            return
        name = update.message.text.strip()
        if len(name) < 2:
            await update.message.reply_text("Имя слишком короткое. Введите минимум 2 символа.")
            return
        try:
            saved = await set_user_profile_name(update.effective_user.id, name)
        except ValueError as e:
            if str(e) == "duplicate profile name":
                await update.message.reply_text("Это имя профиля уже занято, выберите другое.")
            else:
                await update.message.reply_text("Не удалось сохранить имя профиля.")
            return
        context.user_data.pop("await_profile_name", None)
        await update.message.reply_text(f"Имя профиля сохранено: {saved}", reply_markup=main_menu_keyboard())
        return

    if context.user_data.get("await_friend_name"):
        if update.message is None or not update.message.text or update.effective_user is None:
            return
        profile_name = update.message.text.strip()
        if len(profile_name) < 2:
            await update.message.reply_text("Имя профиля слишком короткое.")
            return
        target = await user_id_by_profile_name(profile_name)
        if target is None:
            await update.message.reply_text("Пользователь с таким именем профиля не найден.")
            return
        uid = update.effective_user.id
        try:
            req = await create_friend_request(uid, target)
        except ValueError as e:
            code = str(e)
            if code == "cannot_add_self":
                await update.message.reply_text("Нельзя добавить самого себя.")
            elif code == "target_not_activated":
                await update.message.reply_text(
                    "Этому пользователю пока нельзя отправить приглашение: он ещё не активировал бота. "
                    "Попросите его сначала зайти в бота и нажать /start."
                )
            elif code == "already_friends":
                await update.message.reply_text("Вы уже друзья.")
            else:
                await update.message.reply_text("Не удалось отправить заявку.")
            return
        context.user_data.pop("await_friend_name", None)
        await update.message.reply_text("Заявка отправлена.", reply_markup=main_menu_keyboard())
        if req.status == "pending":
            try:
                from_name = await resolve_profile_name(uid)
                await context.bot.send_message(
                    chat_id=target,
                    text=f"Вам пришла заявка в друзья от «{from_name}».",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("Принять", callback_data=f"fr:acc:{req.id}"),
                                InlineKeyboardButton("Отклонить", callback_data=f"fr:rej:{req.id}"),
                            ]
                        ]
                    ),
                )
            except Exception as e:
                log.warning("notify target friend request failed: %s", e)
        return

    if not context.user_data.get("await_tz_offset"):
        return
    if update.message is None or not update.message.text or update.effective_user is None:
        return
    h = _parse_tz_offset_hours(update.message.text.strip())
    if h is None:
        await update.message.reply_text("Нужно смещение, например +3, -4 или 0 (от −12 до +14).")
        return
    try:
        tz = await set_user_timezone_offset_hours(update.effective_user.id, h)
    except ValueError:
        await update.message.reply_text("Не удалось сохранить.")
        return
    context.user_data.pop("await_tz_offset", None)
    await update.message.reply_text(
        f"Пояс: {format_tz_label(tz)}",
        reply_markup=main_menu_keyboard(),
    )


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


async def _get_history_reminder(rid: UUID, user_id: int) -> Reminder | None:
    async with SessionLocal() as session:
        r = await session.get(Reminder, rid)
        if r is None or r.user_id != user_id or r.active:
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
    if not isinstance(q.data, str):
        await q.answer("Обнови список — кнопка устарела.", show_alert=True)
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
            try:
                await q.edit_message_text("Ошибка.")
            except BadRequest as e:
                log.warning("on_delete_reminder edit error: %s", e)
                await context.bot.send_message(chat_id=q.message.chat_id, text="Ошибка.")
            return
        await session.execute(
            sql_update(Reminder)
            .where(Reminder.id == rid)
            .values(active=False, closed_at=now)
        )
        await session.commit()
    try:
        await q.edit_message_text("Удалено (в истории).", reply_markup=main_menu_keyboard())
    except BadRequest as e:
        log.warning("on_delete_reminder success edit: %s", e)
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text="Удалено (в истории).",
            reply_markup=main_menu_keyboard(),
        )


async def on_delete_reminder_conv_fb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Во время /new нажали 🗑 в списке — удалить и выйти из диалога."""
    await on_delete_reminder(update, context)
    if update.effective_user:
        _clear_pending(update.effective_user.id)
    context.user_data.clear()
    return ConversationHandler.END


async def on_edit_menu_conv_fb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await on_edit_menu(update, context)
    if update.effective_user:
        _clear_pending(update.effective_user.id)
    context.user_data.clear()
    return ConversationHandler.END


async def on_list_page_conv_fb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await on_list_page(update, context)
    if update.effective_user:
        _clear_pending(update.effective_user.id)
    context.user_data.clear()
    return ConversationHandler.END


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
            f"Дата {picked.strftime('%d.%m.%Y')} ({format_tz_label(tz)}).\n"
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
            await update.message.reply_text(
                "Это время уже прошло. Укажи время в будущем (16 43 или 16:43).",
                reply_markup=time_chips_keyboard(),
            )
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
    if q.data == "nt:back":
        rid_s = context.user_data.get("waiting_edit_time")
        edit_rid = context.user_data.get("edit_reminder_id")
        if rid_s and edit_rid:
            try:
                rid = UUID(edit_rid)
            except ValueError:
                return
            r = await _get_reminder_for_user(rid, update.effective_user.id)
            if r is None or not r.active:
                await q.edit_message_text("Не найдено.")
                return
            context.user_data.pop("waiting_edit_date", None)
            context.user_data.pop("waiting_edit_time", None)
            uid = update.effective_user.id
            _clear_pending(uid)
            _mark_pending(uid)
            context.user_data["edit_reminder_id"] = str(rid)
            y, mo = default_calendar_anchor()
            kb = build_calendar_keyboard(y, mo, "ed")
            await q.edit_message_text("Новая дата:", reply_markup=kb)
        return
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
            f"Дата {picked.strftime('%d.%m.%Y')} ({format_tz_label(tz)}). Отправь время через пробел, например: 16 43",
            reply_markup=None,
        )
        return
    hh = int(tok[:2])
    mm = int(tok[2:])
    t = time(hh, mm)
    local_dt = datetime.combine(picked, t, tzinfo=tz)
    fire_at = local_dt.astimezone(timezone.utc)
    if fire_at <= _utcnow():
        await q.edit_message_text(
            "Это время уже прошло. Выбери другое время в будущем или введи вручную (16 43).",
            reply_markup=time_chips_keyboard(),
        )
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
            CallbackQueryHandler(history_dup_start, pattern=r"^histdup:[0-9a-fA-F-]{36}:\d+$"),
        ],
        states={
            ASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_text)],
            ASK_DATE: [
                CallbackQueryHandler(conv_new_calendar, pattern=r"^(?:nd[pnd]:\d+|noop)$"),
            ],
            ASK_TIME: [
                CallbackQueryHandler(conv_time_back_to_date, pattern=r"^nt:back$"),
                CallbackQueryHandler(conv_time_chip, pattern=r"^nt:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_time),
            ],
            ASK_SPAM: [
                CallbackQueryHandler(conv_spam_back_to_time, pattern=r"^ns:bt$"),
                CallbackQueryHandler(conv_spam_select, pattern=r"^ns:"),
            ],
            ASK_SPAM_CUSTOM: [
                CallbackQueryHandler(conv_spam_custom_back, pattern=r"^ns:bspam$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_spam_custom_msg),
            ],
        },
        fallbacks=[
            # Пока открыт /new, список активных всё ещё в чате — rm:/em:/lp: должны сбрасывать диалог
            CallbackQueryHandler(on_delete_reminder_conv_fb, pattern=r"^rm:"),
            CallbackQueryHandler(on_edit_menu_conv_fb, pattern=r"^em:"),
            CallbackQueryHandler(on_list_page_conv_fb, pattern=r"^lp:\d+$"),
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(conv_cancel_cb, pattern=r"^menu:cancel$"),
            CallbackQueryHandler(conv_hhist_back, pattern=r"^hhist:\d+$"),
            CallbackQueryHandler(
                conv_menu_leave,
                pattern=r"^menu:(list|history|tz|help|main|today|settings|friends)$",
            ),
        ],
    )
    # Важно: ConversationHandler раньше любого menu:* — иначе кнопки меню перехватываются до диалога,
    # а fallback разговора (conv_menu_leave) не срабатывает.
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_tz_offset_text, block=False),
    )
    app.add_handler(CallbackQueryHandler(on_nt_standalone, pattern=r"^nt:"))
    app.add_handler(CallbackQueryHandler(on_orphan_new_calendar_callback, pattern=r"^nd[pnd]:"))
    app.add_handler(
        CallbackQueryHandler(
            on_menu_callback,
            pattern=r"^menu:(list|history|tz|help|main|today|settings|friends)$",
        )
    )
    app.add_handler(CallbackQueryHandler(on_friends_callback, pattern=r"^fr:"))
    app.add_handler(CallbackQueryHandler(on_stq_toggle, pattern=r"^stq:toggle$"))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("timezone", cmd_timezone))
    app.add_handler(CallbackQueryHandler(on_noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_tzo_callback, pattern=r"^tzo:"))
    app.add_handler(CallbackQueryHandler(on_list_page, pattern=r"^lp:\d+$"))
    app.add_handler(CallbackQueryHandler(on_history_page, pattern=r"^hp:\d+$"))
    app.add_handler(CallbackQueryHandler(on_hhist_callback, pattern=r"^hhist:\d+$"))
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
