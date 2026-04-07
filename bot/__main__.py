import asyncio
import logging
import sys

from telegram import MenuButtonWebApp, Update, WebAppInfo
from telegram.error import Conflict
from telegram.ext import Application, ContextTypes
from telegram.request import HTTPXRequest

from bot.config import BOT_TOKEN, WEBAPP_PUBLIC_URL
from bot.database import init_db
from bot.handlers import register_handlers
from bot.reminder_worker import reminder_loop


async def _on_error(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logging.error(
            "Telegram Conflict: два процесса одновременно вызывают getUpdates с этим BOT_TOKEN. "
            "Останови локальный бот, второй деплой или лишнюю реплику; один токен — один poller."
        )
        return
    logging.error("Необработанное исключение", exc_info=err)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def post_init(app: Application) -> None:
        # Polling не совместим с активным webhook; снимаем webhook на всякий случай.
        await app.bot.delete_webhook(drop_pending_updates=True)
        await init_db()
        if WEBAPP_PUBLIC_URL:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Приложение",
                    web_app=WebAppInfo(url=WEBAPP_PUBLIC_URL),
                ),
            )
        asyncio.create_task(reminder_loop(app))

    # Railway / слабый канал до api.telegram.org — увеличиваем таймауты (иначе TimedOut при get_me).
    # При .request(...) нельзя задавать get_updates_*_timeout по отдельности — отдельный HTTPXRequest для getUpdates.
    def _http() -> HTTPXRequest:
        return HTTPXRequest(
            connect_timeout=25.0,
            read_timeout=45.0,
            write_timeout=45.0,
            pool_timeout=25.0,
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(_http())
        .get_updates_request(_http())
        .post_init(post_init)
        .build()
    )
    register_handlers(app)
    app.add_error_handler(_on_error)
    print("Bot polling…", file=sys.stderr)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    main()
