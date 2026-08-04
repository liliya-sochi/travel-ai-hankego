"""
Unit-тесты строгого Structured Output.
"""

import json

import pytest
from pydantic import ValidationError

from app.services.ai import (
    AIServiceError,
    _build_request_payload,
    _build_response_format,
    _extract_model_text,
    _validate_trip_plan,
)


def build_valid_trip_data() -> dict[str, object]:
    """
    Возвращает корректный тестовый маршрут.
    """

    return {
        "destination": "Стамбул",
        "duration_days": 2,
        "summary": (
            "Два дня для знакомства "
            "с историей и кухней Стамбула."
        ),
        "days": [
            {
                "day": 1,
                "title": "Исторический центр",
                "morning": [
                    "Прогулка по району Султанахмет."
                ],
                "afternoon": [
                    "Посещение исторических кварталов."
                ],
                "evening": [
                    "Ужин с блюдами турецкой кухни."
                ],
            },
            {
                "day": 2,
                "title": "Босфор и современный город",
                "morning": [
                    "Прогулка вдоль Босфора."
                ],
                "afternoon": [
                    "Знакомство с современными районами."
                ],
                "evening": [
                    "Спокойная прогулка перед отъездом."
                ],
            },
        ],
        "practical_tips": [
            (
                "Проверяйте актуальные часы работы "
                "перед посещением."
            ),
        ],
    }


def test_response_format_uses_strict_json_schema() -> None:
    """
    Проверяет включение строгого JSON Schema mode.
    """

    response_format = _build_response_format()

    assert response_format["type"] == "json_schema"

    json_schema = response_format[
        "json_schema"
    ]

    assert json_schema["name"] == "trip_plan"
    assert json_schema["strict"] is True


def test_all_structured_fields_are_required() -> None:
    """
    Проверяет требования strict mode.
    """

    response_format = _build_response_format()

    schema = response_format[
        "json_schema"
    ]["schema"]

    root_properties = schema["properties"]
    root_required = schema["required"]

    assert set(root_required) == set(
        root_properties
    )

    assert (
        schema["additionalProperties"]
        is False
    )

    day_schema = schema["$defs"]["DayPlan"]

    assert set(
        day_schema["required"]
    ) == set(
        day_schema["properties"]
    )

    assert (
        day_schema["additionalProperties"]
        is False
    )


def test_request_payload_contains_response_format() -> None:
    """
    Проверяет передачу схемы AI-провайдеру.
    """

    messages = [
        {
            "role": "user",
            "content": "Маршрут по Стамбулу.",
        }
    ]

    payload = _build_request_payload(
        model="openai/gpt-oss-120b",
        messages=messages,
    )

    assert (
        payload["model"]
        == "openai/gpt-oss-120b"
    )

    assert payload["messages"] == messages

    assert (
        payload["response_format"]
        ["json_schema"]
        ["strict"]
        is True
    )


def test_extract_model_text() -> None:
    """
    Проверяет извлечение текста модели.
    """

    response_data = {
        "choices": [
            {
                "message": {
                    "content": '{"destination":"Стамбул"}',
                }
            }
        ]
    }

    result = _extract_model_text(
        response_data
    )

    assert result == '{"destination":"Стамбул"}'


def test_extract_model_text_handles_refusal() -> None:
    """
    Проверяет безопасную обработку отказа модели.
    """

    response_data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "refusal": (
                        "Unable to process request."
                    ),
                }
            }
        ]
    }

    with pytest.raises(
        AIServiceError
    ):
        _extract_model_text(
            response_data
        )


def test_validate_structured_trip_plan() -> None:
    """
    Проверяет корректный Structured Output.
    """

    model_text = json.dumps(
        build_valid_trip_data(),
        ensure_ascii=False,
    )

    trip_plan = _validate_trip_plan(
        model_text
    )

    assert trip_plan.destination == "Стамбул"
    assert trip_plan.duration_days == 2
    assert len(trip_plan.days) == 2


def test_validate_rejects_inconsistent_days() -> None:
    """
    Проверяет дополнительное бизнес-правило.
    """

    trip_data = build_valid_trip_data()

    days = trip_data["days"]

    assert isinstance(days, list)
    assert isinstance(days[1], dict)

    days[1]["day"] = 3

    model_text = json.dumps(
        trip_data,
        ensure_ascii=False,
    )

    with pytest.raises(
        ValidationError
    ):
        _validate_trip_plan(
            model_text
        )