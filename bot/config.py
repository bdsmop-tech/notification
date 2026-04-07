import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = _get_env("BOT_TOKEN")
DATABASE_URL = _get_env("DATABASE_URL")

# Railway / Heroku sometimes use postgres:// — asyncpg needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DEFAULT_TZ = ZoneInfo(os.getenv("DEFAULT_TZ", "Europe/Moscow"))
REMINDER_POLL_SECONDS = float(os.getenv("REMINDER_POLL_SECONDS", "5"))
MIN_SPAM_INTERVAL_SECONDS = int(os.getenv("MIN_SPAM_INTERVAL_SECONDS", "15"))
# Интервал для режима «до Прочитал»
READ_ACK_INTERVAL_SECONDS = int(os.getenv("READ_ACK_INTERVAL_SECONDS", "30"))

# Публичный HTTPS URL Mini App (корень, где открывается index.html), например https://web.up.railway.app
WEBAPP_PUBLIC_URL = os.getenv("WEBAPP_PUBLIC_URL", "").strip() or None
