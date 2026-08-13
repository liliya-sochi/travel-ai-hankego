"""Тесты структурированных параметров поездки."""

import pytest
from pydantic import ValidationError

from app.schemas.trip import TripDraft, TripPreferences


def test_trip_preferences_accept_valid_data() -> None:
    """Проверяет корректные данные и удаление пробелов."""

    preferences = TripPreferences.model_validate(
        {
            "destination": " Стамбул ",
            "duration_days": 5,
            "travel_period": " В октябре ",
            "budget": " 150000 ₽ ",
            "interests": " Архитектура и местная еда ",
        }
    )

    assert preferences.destination == "Стамбул"
    assert preferences.duration_days == 5
    assert preferences.travel_period == "В октябре"
    assert preferences.budget == "150000 ₽"
    assert preferences.interests == "Архитектура и местная еда"


def test_trip_preferences_accept_only_required_fields() -> None:
    """Проверяет необязательность периода, бюджета и интересов."""

    preferences = TripPreferences(
        destination="Япония",
        duration_days=7,
    )

    assert preferences.travel_period is None
    assert preferences.budget is None
    assert preferences.interests is None


def test_trip_draft_accepts_incomplete_data() -> None:
    """Проверяет черновик с частично заполненными параметрами."""

    draft = TripDraft(
        destination=" Япония ",
    )

    assert draft.destination == "Япония"
    assert draft.duration_days is None
    assert draft.travel_period is None
    assert draft.budget is None
    assert draft.interests is None


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
