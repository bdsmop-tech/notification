"""Inline-клавиатуры (callback_data ≤ 64 байт)."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Новое", callback_data="menu:new"),
                InlineKeyboardButton("📋 Активные", callback_data="menu:list"),
            ],
            [
                InlineKeyboardButton("📅 Сегодня", callback_data="menu:today"),
                InlineKeyboardButton("📜 История", callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton("🌐 Часовой пояс", callback_data="menu:tz"),
                InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("❓ Помощь", callback_data="menu:help")],
        ]
    )


def time_chips_keyboard() -> InlineKeyboardMarkup:
    """Быстрый выбор времени (ЧЧММ)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("09:00", callback_data="nt:0900"),
                InlineKeyboardButton("12:00", callback_data="nt:1200"),
                InlineKeyboardButton("15:00", callback_data="nt:1500"),
            ],
            [
                InlineKeyboardButton("18:00", callback_data="nt:1800"),
                InlineKeyboardButton("21:00", callback_data="nt:2100"),
            ],
            [InlineKeyboardButton("✍️ Ввести время текстом", callback_data="nt:manual")],
        ]
    )


def spam_mode_keyboard() -> InlineKeyboardMarkup:
    """Режим повторов при создании."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1️⃣ Один раз без повтора", callback_data="ns:0")],
            [
                InlineKeyboardButton(
                    '🔔 Каждые 30 сек до «Прочитал»',
                    callback_data="ns:read30",
                )
            ],
            [
                InlineKeyboardButton("🔁 Каждые 30 сек (Стоп)", callback_data="ns:30"),
                InlineKeyboardButton("🔁 Каждые 60 сек", callback_data="ns:60"),
            ],
            [
                InlineKeyboardButton("🔁 Каждые 120 сек", callback_data="ns:120"),
            ],
            [InlineKeyboardButton("⌨️ Свой интервал (сек)…", callback_data="ns:custom")],
            [InlineKeyboardButton("« Отмена", callback_data="menu:cancel")],
        ]
    )


def edit_spam_keyboard(reminder_id: str) -> InlineKeyboardMarkup:
    """Редактирование режима спама (id без дефисов нельзя — uuid целиком)."""
    rid = str(reminder_id)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1️⃣ Один раз", callback_data=f"ens:{rid}:0")],
            [InlineKeyboardButton('До «Прочитал» (30 сек)', callback_data=f"ens:{rid}:r30")],
            [
                InlineKeyboardButton("30 сек", callback_data=f"ens:{rid}:30"),
                InlineKeyboardButton("60 сек", callback_data=f"ens:{rid}:60"),
            ],
            [InlineKeyboardButton("« Назад", callback_data=f"em:{rid}")],
        ]
    )


def settings_keyboard(quiet_on: bool) -> InlineKeyboardMarkup:
    q = "Вкл ✅" if quiet_on else "Выкл ⛔"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🌙 Тихие часы 23:00–07:00: {q}", callback_data="stq:toggle")],
            [InlineKeyboardButton("« Главное меню", callback_data="menu:main")],
        ]
    )


def back_to_menu_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
