"""Проверяет ограничения ID в реальном HTTP-теле создания и редактирования."""

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.services.ai as ai_service
from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.trip import DayPlan, TripPlanResponse, TripPreferences


def build_context(prefix: str = "tokyo") -> TravelContext:
    """Содержит обязательный Google-музей, парки и отсеиваемые кандидаты."""

    places = [
        PlaceCandidate(
            name="Обязательный музей",
            formatted_address="Токио",
            latitude=35.68,
            longitude=139.76,
            categories=["entertainment.museum"],
            source_place_id=f"ChIJ-{prefix}-museum",
            source="google",
            location_source="google",
        ),
        *[
            PlaceCandidate(
                name=f"Парк {number}",
                formatted_address="Токио",
                latitude=35.68 + number * 0.001,
                longitude=139.76,
                categories=["leisure.park"],
                source_place_id=f"{prefix}-park-{number}",
                website="https://example.com/park",
            )
            for number in (1, 2, 3)
        ],
        PlaceCandidate(
            name="Парк 1",
            formatted_address="Токио",
            latitude=35.681,
            longitude=139.76,
            categories=["leisure.park"],
            source_place_id=f"{prefix}-duplicate",
        ),
        PlaceCandidate(
            name="Запасной парк",
            formatted_address="Токио",
            latitude=35.681,
            longitude=139.76,
            categories=["leisure.park"],
            source_place_id=f"{prefix}-unselected",
        ),
    ]
    return TravelContext(
        location=DestinationLocation(
            formatted_name="Токио",
            latitude=35.68,
            longitude=139.76,
            source_place_id="tokyo-destination-id",
        ),
        requested_categories=["leisure.park"],
        places=places,
        fetched_at=datetime.now(UTC),
    )


def build_preferences(*, with_required_place: bool = True) -> TripPreferences:
    """Музей обязателен даже при интересе только к паркам."""

    return TripPreferences(
        destination="Токио",
        duration_days=1,
        interests="Парки",
        must_visit_places=["Обязательный музей"] if with_required_place else [],
    )


def build_plan(prefix: str = "tokyo", *, general_only: bool = False) -> dict[str, Any]:
    """Ответ провайдера независим от построения исходящего prompt."""

    activities = [
        {
            "source_place_id": f"ChIJ-{prefix}-museum",
            "place_name": "Обязательный музей",
            "activity_focus": "museum",
            "description": None,
        },
        *[
            {
                "source_place_id": f"{prefix}-park-{number}",
                "place_name": f"Парк {number}",
                "activity_focus": "park",
                "description": None,
            }
            for number in (1, 2)
        ],
    ]
    if general_only:
        activities = [
            {
                "source_place_id": None,
                "place_name": None,
                "activity_focus": None,
                "description": "Спокойный отдых.",
            }
            for _ in range(3)
        ]
    return {
        "destination": "Токио",
        "duration_days": 1,
        "summary": "Однодневный маршрут по Токио.",
        "days": [
            {
                "day": 1,
                "title": "План дня",
                "morning": [activities[0]],
                "afternoon": [activities[1]],
                "evening": [activities[2]],
            }
        ],
    }


def mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Перехватывает сериализованный HTTP-запрос, без сети и ключа Groq."""

    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(responses.pop(0))},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        ai_service.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    settings = SimpleNamespace(
        llm_base_url="https://provider.example/openai/v1",
        llm_api_key="test-key",
        llm_model="openai/gpt-oss-120b",
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: settings)
    return requests


def schema_place_ids(payload: dict[str, Any]) -> list[str]:
    """Извлекает enum поля ссылки на место из HTTP-тела."""

    branches = payload["response_format"]["json_schema"]["schema"]["$defs"][
        "GroundedActivity"
    ]["properties"]["source_place_id"]["anyOf"]
    assert branches[1] == {"type": "null"}
    return branches[0]["enum"]


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_schema_ids_match_selected_prompt_places(
    monkeypatch: pytest.MonkeyPatch,
    editing: bool,
) -> None:
    """Создание и редактирование не допускают ID, удалённые из shortlist."""

    requests = mock_provider(monkeypatch, [build_plan()])
    preferences = build_preferences()
    context = build_context()
    if editing:
        current_plan = TripPlanResponse(
            destination="Токио",
            duration_days=1,
            summary="Старый маршрут.",
            days=[
                DayPlan(
                    day=1,
                    title="План дня",
                    morning=["Старое место old-route-id."],
                    afternoon=["Отдых."],
                    evening=["Отдых."],
                )
            ],
            practical_tips=[],
        )
        result = await ai_service.generate_edited_trip_plan(
            preferences=preferences,
            travel_context=context,
            current_plan=current_plan,
            instruction="Добавь парки вместо старого места.",
        )
    else:
        result = await ai_service.generate_trip_plan(
            preferences=preferences,
            travel_context=context,
        )

    assert len(requests) == 1
    payload = requests[0]
    prompt = json.loads(payload["messages"][1]["content"])
    selected_ids = [
        place["source_place_id"] for place in prompt["travel_context"]["places"]
    ]
    assert schema_place_ids(payload) == selected_ids
    assert set(selected_ids) == {
        "ChIJ-tokyo-museum",
        "tokyo-park-1",
        "tokyo-park-2",
        "tokyo-park-3",
    }
    assert prompt["travel_context"]["must_visit_place_ids"] == ["ChIJ-tokyo-museum"]
    assert "Обязательный музей" in result.days[0].morning[0]
    assert "Парк 2" in result.days[0].evening[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_field", ["source_place_id", "place_name"])
async def test_server_validation_and_retry_keep_same_id_constraints(
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
) -> None:
    """Python отвергает неизвестный ID и неверное имя, даже если провайдер их вернул."""

    valid_plan = build_plan()
    invalid_plan = copy.deepcopy(valid_plan)
    invalid_plan["days"][0]["morning"][0][invalid_field] = "unknown-place"
    requests = mock_provider(monkeypatch, [invalid_plan, valid_plan])

    result = await ai_service.generate_trip_plan(
        preferences=build_preferences(),
        travel_context=build_context(),
    )

    assert len(requests) == 2
    assert schema_place_ids(requests[0]) == schema_place_ids(requests[1])
    assert len(requests[1]["messages"]) > len(requests[0]["messages"])
    assert "unknown-place" not in result.model_dump_json()
    assert "Обязательный музей" in result.days[0].morning[0]


@pytest.mark.asyncio
async def test_empty_context_sends_null_only_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без найденных мест API может сформировать план из общих активностей."""

    requests = mock_provider(monkeypatch, [build_plan(general_only=True)])
    result = await ai_service.generate_trip_plan(
        preferences=build_preferences(with_required_place=False),
        travel_context=build_context().model_copy(update={"places": []}),
    )

    assert len(requests) == 1
    schema = requests[0]["response_format"]["json_schema"]["schema"]
    id_schema = schema["$defs"]["GroundedActivity"]["properties"]["source_place_id"]
    assert id_schema["type"] == "null"
    assert "enum" not in id_schema
    assert "anyOf" not in id_schema
    assert result.days[0].morning == ["Спокойный отдых."]


@pytest.mark.asyncio
async def test_consecutive_requests_do_not_share_allowed_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Смена контекста не оставляет ID предыдущего маршрута в схеме."""

    requests = mock_provider(monkeypatch, [build_plan("first"), build_plan("second")])
    for prefix in ("first", "second"):
        await ai_service.generate_trip_plan(
            preferences=build_preferences(),
            travel_context=build_context(prefix),
        )

    assert len(requests) == 2
    assert set(schema_place_ids(requests[0])).isdisjoint(schema_place_ids(requests[1]))
    assert all("first" in place_id for place_id in schema_place_ids(requests[0]))
    assert all("second" in place_id for place_id in schema_place_ids(requests[1]))
