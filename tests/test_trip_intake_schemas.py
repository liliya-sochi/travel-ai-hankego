"""
Тесты Pydantic-контрактов conversational trip intake.
"""

import pytest
from pydantic import ValidationError

from app.schemas.trip import (
    TripDraft,
    TripIntakeExtraction,
    TripIntakeRequest,
    TripIntakeResponse,
)


def build_extraction_data() -> dict[str, object]:
    """Возвращает полный Structured Output с неизвестными полями."""

    return {
        "intent": "plan_trip",
        "destination": "Япония",
        "duration_days": None,
        "travel_period": "Осенью",
        "budget": None,
        "interests": None,
    }


def test_intake_extraction_accepts_nullable_fields() -> None:
    """Проверяет полный Structured Output со значениями null."""

    extraction = TripIntakeExtraction.model_validate(build_extraction_data())

    assert extraction.intent == "plan_trip"
    assert extraction.destination == "Япония"
    assert extraction.duration_days is None
    assert extraction.travel_period == "Осенью"


def test_intake_extraction_requires_every_field() -> None:
    """Проверяет обязательное присутствие всех полей в JSON."""

    extraction_data = build_extraction_data()
    extraction_data.pop("budget")

    with pytest.raises(ValidationError):
        TripIntakeExtraction.model_validate(extraction_data)


def test_intake_json_schema_is_strict() -> None:
    """Проверяет требования JSON Schema для Structured Output."""

    json_schema = TripIntakeExtraction.model_json_schema()

    assert set(json_schema["required"]) == set(json_schema["properties"])
    assert json_schema["additionalProperties"] is False


def test_intake_request_creates_empty_draft() -> None:
    """Проверяет запрос без ранее собранных параметров."""

    request = TripIntakeRequest(
        telegram_id=9000000001,
        user_message=" Хочу на неделю в Японию ",
    )

    assert request.user_message == "Хочу на неделю в Японию"
    assert request.draft == TripDraft()


def test_intake_request_rejects_long_message() -> None:
    """Проверяет ограничение пользовательского ввода."""

    with pytest.raises(ValidationError):
        TripIntakeRequest(
            telegram_id=9000000001,
            user_message="a" * 2001,
        )


def test_intake_response_accepts_ready_draft() -> None:
    """Проверяет ответ, готовый к генерации маршрута."""

    response = TripIntakeResponse(
        intent="plan_trip",
        draft=TripDraft(
            destination="Япония",
            duration_days=7,
        ),
        missing_required_fields=[],
        ready_to_generate=True,
        next_question=None,
    )

    assert response.ready_to_generate is True
    assert response.next_question is None
