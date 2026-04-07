import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import Application
from telegram.request import HTTPXRequest

from bot.config import BOT_TOKEN
from bot.database import init_db
from bot.handlers import register_handlers
from bot.reminder_worker import reminder_loop


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def post_init(app: Application) -> None:
        await init_db()
        asyncio.create_task(reminder_loop(app))

    # Railway / слабый канал до api.telegram.org — увеличиваем таймауты (иначе TimedOut при get_me).
    request = HTTPXRequest(
        connect_timeout=25.0,
        read_timeout=45.0,
        write_timeout=45.0,
        pool_timeout=25.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )
    register_handlers(app)
    print("Bot polling…", file=sys.stderr)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        read_timeout=45.0,
        connect_timeout=25.0,
        write_timeout=45.0,
        pool_timeout=25.0,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
