"""Unit-тесты разбора инструкции редактирования маршрута."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.ai as ai_service
from app.schemas.trip import TripPlanResponse, TripPreferences
from app.services.ai import LLMProviderResponse, analyze_trip_edit


class DummyAsyncClient:
    """Контекстный менеджер для замокированного HTTP-клиента."""

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def build_plan() -> TripPlanResponse:
    """Возвращает минимальный сохранённый маршрут."""

    return TripPlanResponse(
        destination="Токио",
        duration_days=1,
        summary="Один день в Токио.",
        days=[
            {
                "day": 1,
                "title": "Токио",
                "morning": ["三菱一号館"],
                "afternoon": ["Парк"],
                "evening": ["Отдых"],
            }
        ],
        practical_tips=["Проверяйте расписание."],
    )


@pytest.mark.asyncio
async def test_analyze_trip_edit_returns_full_preference_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Требуемое новое место попадает в отдельный строгий список."""

    captured_payload: dict[str, Any] = {}

    async def fake_request_model(**arguments: Any) -> LLMProviderResponse:
        captured_payload.update(arguments["payload"])
        return LLMProviderResponse(
            data={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "supported": True,
                                    "interests_changed": True,
                                    "interests": "Архитектура и музеи",
                                    "must_visit_places_changed": True,
                                    "must_visit_places": ["三鷹の森ジブリ美術館"],
                                },
                                ensure_ascii=False,
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
            duration_ms=25,
            header_request_id="request-id",
        )

    monkeypatch.setattr(
        ai_service,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.com/v1",
            llm_api_key="private",
            llm_model="test-model",
        ),
    )
    monkeypatch.setattr(ai_service, "_request_model", fake_request_model)
    monkeypatch.setattr(ai_service.httpx, "AsyncClient", DummyAsyncClient)

    preferences = TripPreferences(
        destination="Токио",
        duration_days=1,
        interests="Архитектура",
    )
    analysis = await analyze_trip_edit(
        current_preferences=preferences,
        current_plan=build_plan(),
        instruction="Добавь музей Гибли (三鷹の森ジブリ美術館)",
    )

    assert analysis.supported is True
    assert analysis.interests == "Архитектура и музеи"
    assert analysis.must_visit_places == ["三鷹の森ジブリ美術館"]

    response_format = captured_payload["response_format"]
    assert response_format["json_schema"]["name"] == "trip_edit_analysis"
    assert response_format["json_schema"]["strict"] is True

    user_payload = json.loads(captured_payload["messages"][1]["content"])
    assert user_payload["current_preferences"] == preferences.model_dump(mode="json")
    assert user_payload["edit_instruction"] == (
        "Добавь музей Гибли (三鷹の森ジブリ美術館)"
    )


@pytest.mark.asyncio
async def test_analyze_trip_edit_retries_old_plan_place_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не превращает место из старого плана в обязательное требование."""

    response_places = [
        ["三菱一号館", "目黒寄生虫館"],
        ["目黒寄生虫館"],
    ]
    request_count = 0

    async def fake_request_model(**_: Any) -> LLMProviderResponse:
        nonlocal request_count
        must_visit_places = response_places[request_count]
        request_count += 1

        return LLMProviderResponse(
            data={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "supported": True,
                                    "interests_changed": True,
                                    "interests": "Архитектура, парки и музеи",
                                    "must_visit_places_changed": True,
                                    "must_visit_places": must_visit_places,
                                },
                                ensure_ascii=False,
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
            duration_ms=25,
            header_request_id="request-id",
        )

    monkeypatch.setattr(
        ai_service,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.com/v1",
            llm_api_key="private",
            llm_model="test-model",
        ),
    )
    monkeypatch.setattr(ai_service, "_request_model", fake_request_model)
    monkeypatch.setattr(ai_service.httpx, "AsyncClient", DummyAsyncClient)

    analysis = await analyze_trip_edit(
        current_preferences=TripPreferences(
            destination="Токио",
            duration_days=1,
            interests="Архитектура и парки",
        ),
        current_plan=build_plan(),
        instruction=(
            "Добавь Музей паразитологии Мэгуро (目黒寄生虫館) и сделай вечер спокойнее."
        ),
    )

    assert request_count == 2
    assert analysis.must_visit_places == ["目黒寄生虫館"]
