"""Тесты структурированного ввода Telegram-бота."""

from typing import Any

import pytest

import app.bot.api_client as api_client
from app.schemas.trip import (
    TripDraft,
    TripIntakeResponse,
    TripPreferences,
)


@pytest.mark.asyncio
async def test_create_trip_plan_serializes_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет payload Telegram-клиента без готового prompt."""

    captured_arguments: dict[str, Any] = {}

    async def fake_request_backend(
        **arguments: Any,
    ) -> dict[str, Any]:
        captured_arguments.update(arguments)
        return {"status": "ok"}

    monkeypatch.setattr(
        api_client,
        "_request_backend",
        fake_request_backend,
    )

    preferences = TripPreferences(
        destination="Стамбул",
        duration_days=5,
        budget="150000 ₽",
        interests="Архитектура и местная еда",
    )

    result = await api_client.create_trip_plan(
        telegram_id=9000000001,
        first_name="Liliya",
        preferences=preferences,
    )

    assert result == {"status": "ok"}
    assert captured_arguments["payload"] == {
        "telegram_id": 9000000001,
        "first_name": "Liliya",
        "preferences": preferences.model_dump(mode="json"),
    }
    assert "prompt" not in captured_arguments["payload"]


@pytest.mark.asyncio
async def test_process_trip_intake_serializes_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет запрос Telegram-клиента к conversational intake."""

    captured_arguments: dict[str, Any] = {}

    response_data = {
        "intent": "plan_trip",
        "draft": {
            "destination": "Япония",
            "duration_days": 7,
            "travel_period": "Осенью",
            "budget": None,
            "interests": None,
        },
        "missing_required_fields": [],
        "ready_to_generate": True,
        "next_question": None,
    }

    async def fake_request_backend(
        **arguments: Any,
    ) -> dict[str, Any]:
        captured_arguments.update(arguments)
        return response_data

    monkeypatch.setattr(
        api_client,
        "_request_backend",
        fake_request_backend,
    )

    current_draft = TripDraft(
        destination="Япония",
    )

    result = await api_client.process_trip_intake(
        telegram_id=9000000001,
        user_message="На неделю осенью",
        draft=current_draft,
    )

    assert result == TripIntakeResponse.model_validate(response_data)

    assert captured_arguments["method"] == "POST"
    assert captured_arguments["path"] == "/trip-intake"
    assert captured_arguments["payload"] == {
        "telegram_id": 9000000001,
        "user_message": "На неделю осенью",
        "draft": current_draft.model_dump(mode="json"),
    }
    assert captured_arguments["request_timeout"] == 150.0


@pytest.mark.asyncio
async def test_process_trip_intake_rejects_invalid_backend_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не пропускает неполный ответ backend в Telegram-обработчик."""

    async def fake_request_backend(
        **arguments: Any,
    ) -> dict[str, Any]:
        return {
            "intent": "plan_trip",
        }

    monkeypatch.setattr(
        api_client,
        "_request_backend",
        fake_request_backend,
    )

    with pytest.raises(
        api_client.BackendError,
        match="неправильном формате",
    ):
        await api_client.process_trip_intake(
            telegram_id=9000000001,
            user_message="Хочу в Японию",
            draft=TripDraft(),
        )
