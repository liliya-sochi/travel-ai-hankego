"""
Запуск Telegram-бота HankeGo.

Бот использует Redis для хранения FSM-состояний
и передаёт создание маршрута FastAPI backend.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.handlers.history import router as history_router
from app.bot.handlers.plan import router as plan_router
from app.bot.handlers.start import router as start_router
from app.bot.middleware import CorrelationIdMiddleware
from app.config import get_settings
from app.core.logging import configure_logging


logger = logging.getLogger(__name__)


async def run_bot() -> None:
    """
    Создаёт Telegram-бота и запускает polling.
    """

    settings = get_settings()
    configure_logging(settings.log_level)

    telegram_bot = Bot(
        token=settings.telegram_bot_token,
    )

    # RedisStorage хранит состояния диалога и собранные ответы.
    storage = RedisStorage.from_url(
        settings.redis_url,
    )

    # Dispatcher передаёт события подходящим Router.
    dispatcher = Dispatcher(
        storage=storage,
    )

    # Создаём один correlation ID для каждого Telegram update.
    dispatcher.update.outer_middleware(
        CorrelationIdMiddleware()
    )

    dispatcher.include_router(start_router)
    dispatcher.include_router(plan_router)
    dispatcher.include_router(history_router)

    try:
        # Удаляем старый webhook перед запуском polling.
        await telegram_bot.delete_webhook(
            drop_pending_updates=True,
        )

        logger.info("HankeGo Telegram bot started")

        await dispatcher.start_polling(telegram_bot)

    except Exception:
        logger.exception(
            "HankeGo Telegram bot stopped unexpectedly"
        )
        raise

    finally:
        logger.info("HankeGo Telegram bot is stopping")

        # Закрываем соединение с Redis.
        await storage.close()

        # Закрываем HTTP-сессию Telegram.
        await telegram_bot.session.close()

        logger.info("HankeGo Telegram bot stopped")


if __name__ == "__main__":
    asyncio.run(run_bot())