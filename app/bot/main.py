"""
Точка запуска Telegram-бота HankeGo.

Модуль создаёт подключение к Telegram,
регистрирует обработчики сообщений
и запускает получение обновлений через long polling.
"""

import asyncio

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.bot.api_client import get_project_info, send_echo
from app.config import settings


# Dispatcher получает обновления от Telegram
# и передаёт их подходящим обработчикам.
dispatcher = Dispatcher()


@dispatcher.message(CommandStart())
async def handle_start(message: Message) -> None:
    """
    Обрабатывает команду /start.
    """

    await message.answer(
        "Привет! Я HankeGo — твой будущий AI-помощник для путешествий."
    )


@dispatcher.message(Command("info"))
async def handle_info(message: Message) -> None:
    """
    Получает информацию из backend и отправляет её пользователю.
    """

    try:
        project_info = await get_project_info()
    except httpx.HTTPError:
        await message.answer(
            "Backend HankeGo сейчас недоступен. Попробуй немного позже."
        )
        return

    await message.answer(
        f"Проект: {project_info['project']}\n"
        f"Версия: {project_info['version']}\n"
        f"Окружение: {project_info['environment']}"
    )


@dispatcher.message(Command("echo"))
async def handle_echo(message: Message) -> None:
    """
    Передаёт текст пользователя в backend.
    """

    command_parts = message.text.split(maxsplit=1)

    if len(command_parts) < 2:
        await message.answer(
            "Напиши текст после команды.\n"
            "Например: /echo Привет, HankeGo!"
        )
        return

    user_text = command_parts[1]

    try:
        echo_response = await send_echo(user_text)
    except httpx.HTTPError:
        await message.answer(
            "Не удалось получить ответ от backend."
        )
        return

    await message.answer(echo_response["message"])


async def main() -> None:
    """
    Создаёт Telegram-бота и запускает long polling.
    """

    # Извлекаем настоящее значение токена только перед созданием Bot.
    token = settings.telegram_bot_token.get_secret_value()
    bot = Bot(token=token)

    # С этого момента бот постоянно ожидает новые сообщения.
    await dispatcher.start_polling(bot)


# Точку запуска всегда оставляем в самом конце файла,
# когда все обработчики уже зарегистрированы.
if __name__ == "__main__":
    asyncio.run(main())