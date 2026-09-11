"""
Обработчик команды /start.
"""

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.api_client import (
    BackendError,
    register_telegram_user,
)
from app.bot.keyboards import build_main_menu_keyboard

logger = logging.getLogger(__name__)

router = Router()

HELP_MESSAGE = (
    "Опишите поездку обычным сообщением — город, количество дней "
    "и ваши интересы.\n\n"
    "После готового маршрута можно сразу нажать:\n"
    "• «✏️ Подправить» — написать, что изменить;\n"
    "• «🔄 Другой вариант» — получить новую версию с теми же параметрами.\n\n"
    "Кнопка «🧳 Мои маршруты» открывает историю, а «Отмена» "
    "завершает текущий диалог."
)


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


@router.message(Command("help"))
async def help_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """Показывает основные действия и возвращает главное меню."""

    await state.clear()
    await message.answer(
        HELP_MESSAGE,
        reply_markup=build_main_menu_keyboard(),
    )
