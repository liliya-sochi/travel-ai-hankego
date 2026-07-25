"""
Запуск Telegram-бота HankeGo.

Доступные команды:

/start
    Показывает краткую инструкцию.

/plan <описание поездки>
    Создаёт маршрут через FastAPI и Groq.
"""

import asyncio

from aiogram import Bot, Dispatcher, dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message

from app import bot
from app.bot.handlers.start import router as start_router
from app.bot.handlers.plan import router as plan_router
from app.config import get_settings


async def run_bot() -> None:
    """
    Создаёт Telegram-бота и запускает получение сообщений.
    """

    settings = get_settings()

    bot = Bot(
        token=settings.telegram_bot_token,
    )

    # RedisStorage хранит состояния диалога и собранные ответы.
    storage = RedisStorage.from_url(
        settings.redis_url,
    )

    # Dispatcher управляет получением событий от Telegram
    # и передаёт их подходящим обработчикам Router.
    dispatcher = Dispatcher(
        storage=storage,
    )
    dispatcher.include_router(start_router)
    dispatcher.include_router(plan_router)

    # Удаляем старый webhook, если он когда-либо был настроен.
    # Для локального запуска используем polling.
    await bot.delete_webhook(
        drop_pending_updates=True,
    )

    try:
        # start_polling работает постоянно и получает
        # новые сообщения от Telegram.
        await dispatcher.start_polling(bot)

    finally:
        # Закрываем соединение с Redis.
        await storage.close()

        # Корректно закрываем сетевое соединение Telegram.
        await bot.session.close()


if __name__ == "__main__":
    # asyncio.run запускает асинхронную функцию run_bot.
    asyncio.run(run_bot())