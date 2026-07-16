"""
Точка запуска Telegram-бота HankeGo.

Модуль создаёт подключение к Telegram,
регистрирует обработчики сообщений
и запускает получение обновлений через long polling.
"""

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.config import settings


# Dispatcher получает обновления от Telegram
# и передаёт их подходящим обработчикам.
dispatcher = Dispatcher()


@dispatcher.message(CommandStart())
async def handle_start(message: Message) -> None:
    """
    Обрабатывает команду /start.

    Пока бот только подтверждает, что он запущен
    и готов принимать команды пользователя.
    """

    await message.answer(
        "Привет! Я HankeGo — твой будущий AI-помощник для путешествий."
    )


async def main() -> None:
    """
    Создаёт Telegram-бота и запускает long polling.
    """

    # Извлекаем реальное значение токена из SecretStr
    # только непосредственно перед созданием Bot.
    token = settings.telegram_bot_token.get_secret_value()

    bot = Bot(token=token)

    # Long polling постоянно получает новые обновления
    # и передаёт их объекту Dispatcher.
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())