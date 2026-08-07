"""Тесты структурированных параметров поездки."""

import pytest
from pydantic import ValidationError

from app.schemas.trip import TripPreferences


def test_trip_preferences_accept_valid_data() -> None:
    """Проверяет корректные данные и удаление пробелов."""

    preferences = TripPreferences.model_validate(
        {
            "destination": " Стамбул ",
            "duration_days": 5,
            "budget": " 150000 ₽ ",
            "interests": " Архитектура и местная еда ",
        }
    )

    assert preferences.destination == "Стамбул"
    assert preferences.duration_days == 5
    assert preferences.budget == "150000 ₽"
    assert preferences.interests == "Архитектура и местная еда"


@pytest.mark.parametrize(
    "duration_days",
    [
        "7",
        0,
        31,
    ],
)
def test_trip_preferences_reject_invalid_duration(
    duration_days: object,
) -> None:
    """Проверяет тип и допустимые границы длительности."""

    with pytest.raises(ValidationError):
        TripPreferences.model_validate(
            {
                "destination": "Стамбул",
                "duration_days": duration_days,
                "budget": "150000 ₽",
                "interests": "Архитектура и местная еда",
            }
        )


def test_trip_preferences_reject_unknown_field() -> None:
    """Проверяет запрет неожиданных полей."""

    with pytest.raises(ValidationError):
        TripPreferences.model_validate(
            {
                "destination": "Стамбул",
                "duration_days": 5,
                "budget": "150000 ₽",
                "interests": "Архитектура и местная еда",
                "admin": True,
            }
        )