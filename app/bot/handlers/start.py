"""
Обработчик команды /start.
"""

import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.api_client import BackendError, register_telegram_user


logger = logging.getLogger(__name__)

# Router хранит обработчики стартовых сообщений.
router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    """
    Регистрирует пользователя и показывает приветствие.
    """

    telegram_user = message.from_user

    if telegram_user is not None:
        try:
            await register_telegram_user(
                telegram_id=telegram_user.id,
                first_name=telegram_user.first_name,
            )

        except BackendError:
            # Не записываем telegram_id и имя в лог,
            # чтобы не распространять персональные данные.
            logger.exception(
                "Failed to register Telegram user"
            )

    await message.answer(
        "Привет! Я HankeGo — AI-помощник по путешествиям.\n\n"
        "Опиши желаемую поездку после команды /plan.\n\n"
        "Например:\n"
        "/plan Хочу на 5 дней в Стамбул. "
        "Люблю архитектуру, прогулки и местную еду."
    )