"""
Клавиатуры Telegram-бота HankeGo.
"""

from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
)

NEW_TRIP_BUTTON_TEXT = "✈️ Новая поездка"
MY_TRIPS_BUTTON_TEXT = "🧳 Мои маршруты"
CANCEL_BUTTON_TEXT = "❌ Отменить"


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """
    Создаёт постоянное главное меню Telegram-бота.

    Reply-кнопки отправляют текстовое сообщение от имени пользователя.
    Обработчики распознают точный текст и не обращаются к LLM.
    """

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=NEW_TRIP_BUTTON_TEXT,
                ),
                KeyboardButton(
                    text=MY_TRIPS_BUTTON_TEXT,
                ),
            ],
            [
                KeyboardButton(
                    text=CANCEL_BUTTON_TEXT,
                ),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Опишите желаемую поездку...",
    )
