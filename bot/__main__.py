import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import Application

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

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    register_handlers(app)
    print("Bot polling…", file=sys.stderr)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
