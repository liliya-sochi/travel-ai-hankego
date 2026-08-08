"""
Тесты служебных функций Telegram handlers.
"""

from app.bot.handlers.history import (
    DELETE_CONFIRM_PREFIX,
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
