"""Inline calendar (month grid). Callback data ≤ 64 bytes."""

from __future__ import annotations

import calendar as cal_module
from datetime import date, datetime
from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

Prefix = Literal["nd", "ed"]

_MONTH_RU = (
    "",
    "Янв",
    "Фев",
    "Мар",
    "Апр",
    "Май",
    "Июн",
    "Июл",
    "Авг",
    "Сен",
    "Окт",
    "Ноя",
    "Дек",
)


def ym_int(y: int, m: int) -> int:
    return y * 100 + m


def ym_from_int(ym: int) -> tuple[int, int]:
    return ym // 100, ym % 100


def ymd_int(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def date_from_ymd_int(ymd: int) -> date:
    y = ymd // 10000
    rest = ymd % 10000
    m = rest // 100
    day = rest % 100
    return date(y, m, day)


def build_calendar_keyboard(year: int, month: int, prefix: Prefix) -> InlineKeyboardMarkup:
    """prefix nd = new reminder, ed = edit reminder (edit id lives in user_data)."""
    ym = ym_int(year, month)
    nav_row = [
        InlineKeyboardButton("«", callback_data=f"{prefix}p:{ym}"),
        InlineKeyboardButton(
            f"{_MONTH_RU[month]} {year}",
            callback_data="noop",
        ),
        InlineKeyboardButton("»", callback_data=f"{prefix}n:{ym}"),
    ]
    rows: list[list[InlineKeyboardButton]] = [nav_row]

    wd_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    rows.append([InlineKeyboardButton(n, callback_data="noop") for n in wd_names])

    c = cal_module.Calendar(firstweekday=0)
    weeks = c.monthdatescalendar(year, month)
    for week in weeks:
        row: list[InlineKeyboardButton] = []
        for d in week:
            if d.month != month:
                row.append(InlineKeyboardButton(" ", callback_data="noop"))
            else:
                ymd = ymd_int(d)
                row.append(
                    InlineKeyboardButton(
                        str(d.day),
                        callback_data=f"{prefix}d:{ymd}",
                    )
                )
        rows.append(row)

    return InlineKeyboardMarkup(rows)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    d = date(year, month, 1)
    if delta > 0:
        if month == 12:
            return year + 1, 1
        return year, month + 1
    if month == 1:
        return year - 1, 12
    return year, month - 1


def month_from_nav(ym: int, direction: str) -> tuple[int, int]:
    y, m = ym_from_int(ym)
    if direction == "n":
        return shift_month(y, m, 1)
    if direction == "p":
        return shift_month(y, m, -1)
    return y, m


def parse_calendar_callback(data: str, prefix: Prefix) -> tuple[str, int | None]:
    """Returns (action, payload) where action is p|n|d and payload is ym or ymd."""
    if not data.startswith(prefix):
        raise ValueError("bad prefix")
    rest = data[len(prefix) :]
    if rest.startswith("p:"):
        return "p", int(rest[2:])
    if rest.startswith("n:"):
        return "n", int(rest[2:])
    if rest.startswith("d:"):
        return "d", int(rest[2:])
    raise ValueError("bad callback")


def default_calendar_anchor() -> tuple[int, int]:
    now = datetime.now().astimezone()
    return now.year, now.month
