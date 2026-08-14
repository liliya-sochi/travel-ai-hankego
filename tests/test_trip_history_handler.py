"""
Тесты служебных функций Telegram handlers.
"""

from app.bot.handlers.history import (
    DELETE_CONFIRM_PREFIX,
    DELETE_REQUEST_PREFIX,
    OPEN_TRIP_PREFIX,
    build_delete_confirmation_keyboard,
    build_trip_actions_keyboard,
    build_trip_history_keyboard,
    extract_callback_trip_id,
    extract_trip_id,
)


def test_extract_trip_id() -> None:
    """
    Проверяет ID из Telegram-команды.
    """

    assert extract_trip_id("/trip 7") == 7
    assert extract_trip_id("/delete_trip 15") == 15

    assert extract_trip_id("/trip") is None
    assert extract_trip_id("/trip abc") is None
    assert extract_trip_id("/trip -1") is None


def test_extract_callback_trip_id() -> None:
    """
    Проверяет ID из callback data.
    """

    assert (
        extract_callback_trip_id(
            f"{DELETE_CONFIRM_PREFIX}7",
            DELETE_CONFIRM_PREFIX,
        )
        == 7
    )

    assert (
        extract_callback_trip_id(
            f"{DELETE_CONFIRM_PREFIX}abc",
            DELETE_CONFIRM_PREFIX,
        )
        is None
    )

    assert (
        extract_callback_trip_id(
            None,
            DELETE_CONFIRM_PREFIX,
        )
        is None
    )


def test_build_trip_history_keyboard() -> None:
    """
    Создаёт отдельные кнопки для каждого маршрута.
    """

    keyboard = build_trip_history_keyboard(
        [
            {
                "trip_id": 7,
                "destination": "Япония",
            },
            {
                "trip_id": 15,
                "destination": "Италия",
            },
        ]
    )

    assert len(keyboard.inline_keyboard) == 2

    assert keyboard.inline_keyboard[0][0].callback_data == (f"{OPEN_TRIP_PREFIX}7")
    assert keyboard.inline_keyboard[0][1].callback_data == (f"{DELETE_REQUEST_PREFIX}7")

    assert keyboard.inline_keyboard[1][0].callback_data == (f"{OPEN_TRIP_PREFIX}15")
    assert keyboard.inline_keyboard[1][1].callback_data == (
        f"{DELETE_REQUEST_PREFIX}15"
    )


def test_build_trip_actions_keyboard() -> None:
    """
    Добавляет удаление под открытым маршрутом.
    """

    keyboard = build_trip_actions_keyboard(7)

    assert keyboard.inline_keyboard[0][0].callback_data == (f"{DELETE_REQUEST_PREFIX}7")


def test_build_delete_confirmation_keyboard() -> None:
    """
    Удаление выполняется только после отдельного подтверждения.
    """

    keyboard = build_delete_confirmation_keyboard(7)

    assert keyboard.inline_keyboard[0][0].callback_data == (f"{DELETE_CONFIRM_PREFIX}7")
    assert keyboard.inline_keyboard[0][1].callback_data == ("trip_delete_cancel:7")
