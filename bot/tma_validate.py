"""Проверка подписи Telegram Web App / Mini App (initData)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,
) -> dict | None:
    """
    Возвращает распарсенные поля (user — dict) или None, если подпись неверна / данные устарели.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs_list = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        return None
    pairs: dict[str, str] = dict(pairs_list)
    if "hash" not in pairs or "auth_date" not in pairs:
        return None
    tg_hash = pairs.pop("hash")
    try:
        auth_date = int(pairs["auth_date"])
    except (TypeError, ValueError):
        return None
    if time.time() - auth_date > max_age_seconds:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, tg_hash):
        return None
    out: dict = dict(pairs)
    out["auth_date"] = auth_date
    if "user" in out:
        try:
            out["user"] = json.loads(out["user"])
        except (json.JSONDecodeError, TypeError):
            return None
    return out
