"""Тесты структурированного ввода Telegram-бота."""

from typing import Any

import pytest

import app.bot.api_client as api_client
from app.bot.handlers.plan import parse_duration_days
from app.schemas.trip import TripPreferences


def test_parse_duration_days() -> None:
    """Проверяет допустимые числа и отклонение свободного текста."""

    assert parse_duration_days(" 7 ") == 7
    assert parse_duration_days("1") == 1
    assert parse_duration_days("30") == 30

    assert parse_duration_days("0") is None
    assert parse_duration_days("31") is None
    assert parse_duration_days("7 дней") is None
    assert parse_duration_days("1.5") is None
    assert parse_duration_days("") is None


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
