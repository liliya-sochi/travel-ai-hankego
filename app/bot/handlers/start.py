"""
Обработчик команды /start.
"""

import logging

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.api_client import (
    BackendError,
    register_telegram_user,
)
from app.bot.keyboards import build_main_menu_keyboard

logger = logging.getLogger(__name__)

router = Router()


@router.message(CommandStart())
async def start_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Регистрирует пользователя и показывает главное меню.
    """

    # /start начинает взаимодействие заново,
    # поэтому старый незавершённый черновик удаляется.
    await state.clear()

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
            logger.exception("Failed to register Telegram user")

    await message.answer(
        "Привет! Я HankeGo — AI-помощник по путешествиям.\n\n"
        "Просто опишите желаемую поездку обычным сообщением.\n\n"
        "Например:\n"
        "Хочу осенью на неделю в Японию. "
        "Люблю современную архитектуру и местную еду.",
        reply_markup=build_main_menu_keyboard(),
    )
