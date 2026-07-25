"""
Обработчик команды /start.
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


# Router хранит обработчики стартовых сообщений.
router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    """
    Показывает пользователю приветствие и краткую инструкцию.
    """

    await message.answer(
        "Привет! Я HankeGo — AI-помощник по путешествиям.\n\n"
        "Опиши желаемую поездку после команды /plan.\n\n"
        "Например:\n"
        "/plan Хочу на 5 дней в Стамбул. "
        "Люблю архитектуру, прогулки и местную еду."
    )