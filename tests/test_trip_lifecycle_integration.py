"""Сквозные HTTP-сценарии с настоящей PostgreSQL и фиксированным LLM.

Название обязательного музея взято из проверенного сценария Токио.
ID, координаты и расписания здесь условные, не туристические данные.
Подменяются только внешние ответы; сервис, grounding и SQL работают реально.
"""

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.ai as ai_service
from app.api.dependencies import get_trip_enrichment_service
from app.database import get_session
from app.main import app
from app.models.trip import Trip
from app.repositories.user import UserRepository
from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.trip import TripPreferences

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OWNER_ID = 9000000001
OTHER_ID = 9000000002
REQUIRED_MUSEUM = "Mitsubishi Ichigokan Museum"
EDIT_INSTRUCTION = "Вместо парков хочу посещать музеи. Вечером — спокойный отдых."


def preferences() -> TripPreferences:
    """Сохраняет обязательное место отдельно от интересов."""

    return TripPreferences(
        destination="Токио",
        duration_days=1,
        interests="Парки",
        must_visit_places=[REQUIRED_MUSEUM],
    )


def grounded_plan(*, museums: bool = False) -> dict[str, Any]:
    """Возвращает фиксированный ответ, включая недостоверный заголовок LLM."""

    def visit(place_id: str, name: str, focus: str) -> dict[str, Any]:
        return {
            "source_place_id": place_id,
            "place_name": name,
            "activity_focus": focus,
            "description": None,
        }

    return {
        "destination": "Токио",
        "duration_days": 1,
        "summary": "Посещения музеев." if museums else "Музей и два парка.",
        "days": [
            {
                "day": 1,
                "title": "Смотровые площадки",
                "morning": [visit("required", REQUIRED_MUSEUM, "museum")],
                "afternoon": [
                    visit("second-museum", "Тестовый музей", "museum")
                    if museums
                    else visit("day-park", "Тестовый дневной парк", "park")
                ],
                "evening": [
                    {
                        "source_place_id": None,
                        "place_name": None,
                        "activity_focus": None,
                        "description": "Спокойный отдых.",
                    }
                    if museums
                    else visit("evening-park", "Тестовый вечерний парк", "park")
                ],
            }
        ],
    }


def edit_analysis(*, supported: bool = True) -> dict[str, Any]:
    """Даёт полный список обязательных мест при смене интересов."""

    return {
        "supported": supported,
        "interests_changed": supported,
        "interests": "Музеи" if supported else None,
        "must_visit_places_changed": False,
        "must_visit_places": [REQUIRED_MUSEUM],
    }


@dataclass
class ModelReplay:
    """Проверяет порядок внешних вызовов и отдаёт заранее заданные JSON."""

    replies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def request(self, **arguments: Any) -> ai_service.LLMProviderResponse:
        """Останавливает тест при неожиданном обращении к провайдеру."""

        payload = arguments["payload"]
        schema_name = payload["response_format"]["json_schema"]["name"]
        if not self.replies:
            pytest.fail("Незапланированный вызов LLM в сквозном сценарии.")
        expected_schema, output = self.replies.pop(0)
        assert schema_name == expected_schema
        self.calls.append(schema_name)
        return ai_service.LLMProviderResponse(
            data={
                "model": payload["model"],
                "choices": [
                    {
                        "message": {"content": json.dumps(output, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
            duration_ms=1,
            header_request_id="fixture-request",
        )


@dataclass
class ContextReplay:
    """Возвращает новый контекст с учётом обновлённых предпочтений."""

    calls: list[TripPreferences] = field(default_factory=list)

    async def enrich(self, trip_preferences: TripPreferences) -> TravelContext:
        """Подменяет внешние геоданные, сохраняя реальную форму TravelContext."""

        self.calls.append(trip_preferences.model_copy(deep=True))
        place_data = [
            ("required", REQUIRED_MUSEUM, "entertainment.museum"),
            ("second-museum", "Тестовый музей", "entertainment.museum"),
        ]
        categories = ["entertainment.museum"]
        if trip_preferences.interests == "Парки":
            place_data.extend(
                [
                    ("day-park", "Тестовый дневной парк", "leisure.park"),
                    ("evening-park", "Тестовый вечерний парк", "leisure.park"),
                ]
            )
            categories.append("leisure.park")
        return TravelContext(
            location=DestinationLocation(
                formatted_name="Токио",
                latitude=35.68,
                longitude=139.76,
                source_place_id="fixture-tokyo",
            ),
            requested_categories=categories,
            places=[
                PlaceCandidate(
                    source_place_id=place_id,
                    name=name,
                    formatted_address="Условный тестовый адрес",
                    latitude=35.68 + index * 0.001,
                    longitude=139.76,
                    categories=[category],
                    opening_hours="24/7"
                    if category == "leisure.park"
                    else "Mo-Su 10:00-18:00",
                )
                for index, (place_id, name, category) in enumerate(place_data)
            ],
            fetched_at=datetime(2026, 10, 7, tzinfo=UTC),
        )


@dataclass
class LifecycleCase:
    """Объединяет клиент и наблюдаемые внешние границы сценария."""

    client: AsyncClient
    model: ModelReplay
    context: ContextReplay

    async def create(self) -> dict[str, Any]:
        """Создаёт исходный маршрут через настоящий HTTP endpoint."""

        self.model.replies.append(("trip_plan", grounded_plan()))
        response = await self.client.post(
            "/api/v1/trip-plan",
            json={
                "telegram_id": OWNER_ID,
                "first_name": "Fixture User",
                "preferences": preferences().model_dump(mode="json"),
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def details(self, trip_id: int) -> dict[str, Any]:
        """Читает сохранённый маршрут отдельным HTTP-запросом."""

        response = await self.client.post(
            "/api/v1/trip-details",
            json={"telegram_id": OWNER_ID, "trip_id": trip_id},
        )
        assert response.status_code == 200, response.text
        return response.json()


@pytest_asyncio.fixture
async def lifecycle_case(
    database_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[LifecycleCase]:
    """Выдаёт отдельную настоящую сессию PostgreSQL на каждый HTTP-запрос."""

    session_factory = async_sessionmaker(
        bind=database_session.bind,
        expire_on_commit=False,
    )

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    model = ModelReplay()
    context = ContextReplay()
    monkeypatch.setattr(ai_service, "_request_model", model.request)
    overrides = {
        get_session: test_session,
        get_trip_enrichment_service: lambda: context,
    }
    previous = {key: app.dependency_overrides.get(key) for key in overrides}
    app.dependency_overrides.update(overrides)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield LifecycleCase(client=client, model=model, context=context)
    finally:
        for dependency, old_override in previous.items():
            if old_override is None:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = old_override


async def stored_state(
    session: AsyncSession, trip_id: int
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    """Считывает фиксированное состояние из БД вне сессий HTTP-запросов."""

    await session.rollback()
    trip = await session.scalar(select(Trip).where(Trip.id == trip_id))
    assert trip is not None
    count = await session.scalar(select(func.count()).select_from(Trip))
    assert count is not None
    return deepcopy(trip.plan_data), deepcopy(trip.preferences_data), count


async def test_create_edit_and_reopen_keep_required_place_and_same_trip(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
) -> None:
    """Проверяет сохранение, новый заголовок и отсутствие дубликата маршрута."""

    case = lifecycle_case
    created = await case.create()
    trip_id = created["trip_id"]
    original = await case.details(trip_id)
    assert original["days"][0]["title"] == "Музеи и парки"
    case.model.replies.extend(
        [
            ("trip_edit_analysis", edit_analysis()),
            ("trip_plan", grounded_plan(museums=True)),
        ]
    )
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": EDIT_INSTRUCTION,
        },
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["updated"] is True
    assert edited["trip_id"] == trip_id
    assert edited["created_at"] == created["created_at"]
    assert edited["days"][0]["title"] == "Музеи"
    assert REQUIRED_MUSEUM in edited["days"][0]["morning"][0]
    assert "Тестовый музей" in edited["days"][0]["afternoon"][0]
    assert edited["days"][0]["evening"] == ["Спокойный отдых."]
    assert await case.details(trip_id) == {
        key: value for key, value in edited.items() if key != "updated"
    }
    history = await case.client.post(
        "/api/v1/trip-history", json={"telegram_id": OWNER_ID}
    )
    assert history.status_code == 200
    assert [trip["trip_id"] for trip in history.json()["trips"]] == [trip_id]
    plan_data, preference_data, count = await stored_state(database_session, trip_id)
    assert plan_data["days"] == edited["days"]
    assert preference_data == preferences().model_copy(
        update={"interests": "Музеи"}
    ).model_dump(mode="json")
    assert count == 1
    assert case.context.calls == [
        preferences(),
        TripPreferences.model_validate(preference_data),
    ]
    assert case.model.calls == ["trip_plan", "trip_edit_analysis", "trip_plan"]
    assert not case.model.replies


async def test_duration_change_stops_before_enrichment_and_preserves_database(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
) -> None:
    """Ожидаемый 422 не запускает генерацию и не меняет сохранённые требования."""

    case = lifecycle_case
    trip_id = (await case.create())["trip_id"]
    before = await case.details(trip_id)
    state_before = await stored_state(database_session, trip_id)
    case.model.replies.append(("trip_edit_analysis", edit_analysis(supported=False)))
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": "Сделай этот маршрут на два дня.",
        },
    )
    assert response.status_code == 422
    assert "количество дней" in response.json()["detail"]
    assert await case.details(trip_id) == before
    assert await stored_state(database_session, trip_id) == state_before
    assert len(case.context.calls) == 1
    assert case.model.calls == ["trip_plan", "trip_edit_analysis"]
    assert not case.model.replies


@pytest.mark.parametrize("failure", ["schema", "unknown_place", "missing_required"])
async def test_invalid_model_edit_preserves_plan_and_preferences(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
    failure: str,
) -> None:
    """Невалидная новая версия не портит план или исходные preferences в БД."""

    case = lifecycle_case
    trip_id = (await case.create())["trip_id"]
    before = await case.details(trip_id)
    state_before = await stored_state(database_session, trip_id)
    invalid = grounded_plan(museums=True)
    if failure == "schema":
        del invalid["days"]
    elif failure == "unknown_place":
        invalid["days"][0]["afternoon"][0]["source_place_id"] = "invented-place"
    else:
        invalid["days"][0]["morning"] = deepcopy(invalid["days"][0]["afternoon"])
    case.model.replies.append(("trip_edit_analysis", edit_analysis()))
    case.model.replies.extend(
        ("trip_plan", invalid) for _ in range(ai_service.MAX_SEMANTIC_ATTEMPTS)
    )
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": EDIT_INSTRUCTION,
        },
    )
    assert response.status_code == 502
    assert set(response.json()) == {"detail"}
    assert "логически корректный маршрут" in response.json()["detail"]
    assert await case.details(trip_id) == before
    assert await stored_state(database_session, trip_id) == state_before
    assert len(case.context.calls) == 2
    assert len(case.model.calls) == 2 + ai_service.MAX_SEMANTIC_ATTEMPTS
    assert not case.model.replies


async def test_other_user_cannot_read_or_edit_saved_trip(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
) -> None:
    """Чужой маршрут недоступен через реальные SQL-фильтры до вызова LLM."""

    case = lifecycle_case
    trip_id = (await case.create())["trip_id"]
    before = await stored_state(database_session, trip_id)
    await UserRepository(database_session).upsert_telegram_user(
        telegram_id=OTHER_ID, first_name="Other Fixture User"
    )
    await database_session.commit()
    for path in ["trip-details", "trip-edit"]:
        payload = {"telegram_id": OTHER_ID, "trip_id": trip_id}
        if path == "trip-edit":
            payload["instruction"] = EDIT_INSTRUCTION
        response = await case.client.post(f"/api/v1/{path}", json=payload)
        assert response.status_code == 404
        assert response.json() == {"detail": "Маршрут не найден."}
    assert await stored_state(database_session, trip_id) == before
    assert len(case.context.calls) == 1
    assert case.model.calls == ["trip_plan"]
    assert not case.model.replies
