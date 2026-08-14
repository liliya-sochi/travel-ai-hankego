"""
Тесты клавиатур Telegram-бота.
"""

from app.bot.keyboards import (
    CANCEL_BUTTON_TEXT,
    MY_TRIPS_BUTTON_TEXT,
    NEW_TRIP_BUTTON_TEXT,
    build_main_menu_keyboard,
)


def test_main_menu_keyboard() -> None:
    """
    Проверяет состав постоянного главного меню.
    """

    keyboard = build_main_menu_keyboard()

    button_texts = [[button.text for button in row] for row in keyboard.keyboard]

    assert button_texts == [
        [
            NEW_TRIP_BUTTON_TEXT,
            MY_TRIPS_BUTTON_TEXT,
        ],
        [
            CANCEL_BUTTON_TEXT,
        ],
    ]
    assert keyboard.resize_keyboard is True
