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
from app.schemas.trip import RestPeriod, TripPreferences

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
                        "description": "прогулка",
                    }
                    if museums
                    else visit("evening-park", "Тестовый вечерний парк", "park")
                ],
            }
        ],
    }


def edit_analysis(
    *, supported: bool = True, change_rest: bool = True
) -> dict[str, Any]:
    """Даёт полный список обязательных мест при смене интересов."""

    return {
        "period_actions": [],
        "place_moves": [],
        "place_removals": [],
        "place_additions": [],
        "place_replacements": [],
        "requires_regeneration": True,
        "regeneration_periods": [
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
        "supported": supported,
        "interests_changed": supported,
        "interests": "Музеи" if supported else None,
        "must_visit_places_changed": False,
        "rest_period_changes": (
            [{"day": None, "period": "evening", "rest": True}]
            if supported and change_rest
            else []
        ),
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
    preserved_names: list[list[str]] = field(default_factory=list)
    missing_place_id: str | None = None
    replacement_defect: str | None = None
    move_defect: str | None = None

    async def enrich(
        self,
        trip_preferences: TripPreferences,
        *,
        preserved_place_names: list[str] | None = None,
    ) -> TravelContext:
        """Подменяет внешние геоданные, сохраняя реальную форму TravelContext."""

        self.calls.append(trip_preferences.model_copy(deep=True))
        self.preserved_names.append(preserved_place_names or [])
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
        place_data = [item for item in place_data if item[0] != self.missing_place_id]
        context = TravelContext(
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
        if self.move_defect is not None:
            moved = next(p for p in context.places if p.source_place_id == "required")
            if self.move_defect == "closed":
                moved.opening_hours = "Mo-Su 09:00-11:00"
            elif self.move_defect == "far":
                moved.latitude = 35.8
        if self.replacement_defect is not None:
            candidate = next(
                p for p in context.places if p.source_place_id == "second-museum"
            )
            if self.replacement_defect == "far":
                candidate.latitude = 35.8
            else:
                candidate.categories = ["leisure.park"]
            context.places.append(
                PlaceCandidate(
                    source_place_id="near-museum",
                    name="Близкий тестовый музей",
                    formatted_address="Условный тестовый адрес",
                    latitude=35.684,
                    longitude=139.76,
                    categories=["entertainment.museum"],
                    opening_hours="Mo-Su 10:00-18:00",
                )
            )
        return context


@dataclass
class LifecycleCase:
    """Объединяет клиент и наблюдаемые внешние границы сценария."""

    client: AsyncClient
    model: ModelReplay
    context: ContextReplay

    async def create(self, *, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        """Создаёт исходный маршрут через настоящий HTTP endpoint."""

        self.model.replies.append(
            ("trip_plan", plan if plan is not None else grounded_plan())
        )
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
        update={
            "interests": "Музеи",
            "rest_periods": [RestPeriod(day=None, period="evening")],
        }
    ).model_dump(mode="json")
    assert count == 1
    assert case.context.calls == [
        preferences(),
        TripPreferences.model_validate(preference_data),
    ]
    assert case.model.calls == ["trip_plan", "trip_edit_analysis", "trip_plan"]
    assert not case.model.replies

    # Следующая инструкция не повторяет пожелание к отдыху, но оно сохраняется.
    case.model.replies.extend(
        [
            ("trip_edit_analysis", edit_analysis(change_rest=False)),
            ("trip_plan", grounded_plan(museums=True)),
        ]
    )
    second = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": "Оставь утренний музей.",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["days"][0]["evening"] == ["Спокойный отдых."]
    assert (await case.details(trip_id))["days"][0]["evening"] == ["Спокойный отдых."]
    _, second_preferences, second_count = await stored_state(database_session, trip_id)
    assert second_preferences["rest_periods"] == [{"day": None, "period": "evening"}]
    assert second_count == 1
    assert not case.model.replies


async def test_local_rest_edit_persists_same_visits_without_generation(
    lifecycle_case: LifecycleCase, database_session: AsyncSession
) -> None:
    """Настоящие HTTP и PostgreSQL сохраняют только локальную правку отдыха."""

    case = lifecycle_case
    created = await case.create()
    trip_id = created["trip_id"]
    before = await case.details(trip_id)
    parsed = {
        **edit_analysis(),
        "interests_changed": False,
        "interests": None,
        "requires_regeneration": False,
        "regeneration_periods": [],
    }
    case.model.replies.append(("trip_edit_analysis", parsed))
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": "Вечером спокойный отдых. Остальное оставь.",
        },
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["days"][0]["morning"] == before["days"][0]["morning"]
    assert edited["days"][0]["afternoon"] == before["days"][0]["afternoon"]
    assert edited["days"][0]["evening"] == ["Спокойный отдых."]
    assert case.model.calls == ["trip_plan", "trip_edit_analysis"]
    assert case.context.preserved_names[-1] == [
        REQUIRED_MUSEUM,
        "Тестовый дневной парк",
    ]
    _, saved_preferences, count = await stored_state(database_session, trip_id)
    assert count == 1
    assert saved_preferences["must_visit_places"] == preferences().must_visit_places
    assert saved_preferences["rest_periods"] == [{"day": None, "period": "evening"}]
    assert (await case.details(trip_id))["days"] == edited["days"]


async def test_unverifiable_local_edit_does_not_write_database(
    lifecycle_case: LifecycleCase, database_session: AsyncSession
) -> None:
    """Неподтверждённое посещение не заменяется и не меняет сохранённый маршрут."""

    case = lifecycle_case
    created = await case.create()
    trip_id = created["trip_id"]
    before = await stored_state(database_session, trip_id)
    case.context.missing_place_id = "day-park"
    case.model.replies.append(
        (
            "trip_edit_analysis",
            {
                **edit_analysis(),
                "interests_changed": False,
                "interests": None,
                "requires_regeneration": False,
                "regeneration_periods": [],
            },
        )
    )
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": "Вечером спокойный отдых. Остальное оставь.",
        },
    )
    assert response.status_code == 502, response.text
    assert "Маршрут не изменён" in response.text
    assert await stored_state(database_session, trip_id) == before
    assert case.model.calls == ["trip_plan", "trip_edit_analysis"]


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


@pytest.mark.parametrize("replacement_succeeds", [False, True])
@pytest.mark.parametrize("defect", ["old_id", "far", "wrong_category"])
async def test_visit_replacement_never_saves_unchanged_target(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
    replacement_succeeds: bool,
    defect: str,
) -> None:
    """Реальная БД принимает только выполненную замену после ограниченной проверки."""

    case = lifecycle_case
    created = await case.create()
    trip_id = created["trip_id"]
    before = await stored_state(database_session, trip_id)
    if defect != "old_id":
        case.context.replacement_defect = defect
    parsed = {
        **edit_analysis(change_rest=False),
        "interests_changed": False,
        "interests": None,
        "regeneration_periods": [{"day": 1, "period": "afternoon"}],
        "place_replacements": [
            {
                "day": 1,
                "period": "afternoon",
                "place_name": "Тестовый дневной парк",
                "activity_focus": "museum",
                "nearby": defect != "old_id",
            }
        ],
    }
    replacement = grounded_plan()
    replacement_name = (
        "Тестовый музей" if defect == "old_id" else "Близкий тестовый музей"
    )
    replacement["days"][0]["afternoon"] = [
        {
            "source_place_id": "second-museum" if defect == "old_id" else "near-museum",
            "place_name": replacement_name,
            "activity_focus": "museum",
            "description": None,
        }
    ]
    rejected = grounded_plan()
    if defect != "old_id":
        rejected["days"][0]["afternoon"] = [
            {
                "source_place_id": "second-museum",
                "place_name": "Тестовый музей",
                "activity_focus": "park" if defect == "wrong_category" else "museum",
                "description": None,
            }
        ]
    case.model.replies.extend(
        [
            ("trip_edit_analysis", parsed),
            ("trip_plan", rejected),
            ("trip_plan", replacement if replacement_succeeds else rejected),
        ]
    )
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": (
                "Днём замени парк на музей поблизости. Остальные активности оставь."
                if defect != "old_id"
                else "Днём замени парк на музей. Все остальные активности оставь."
            ),
        },
    )
    assert response.status_code == (200 if replacement_succeeds else 502), response.text
    after = await stored_state(database_session, trip_id)
    assert after[1:] == before[1:]
    if replacement_succeeds:
        assert after[0]["days"][0]["afternoon"][0].startswith(
            f"{replacement_name}: посетить музей."
        )
        for period in ("morning", "evening"):
            assert after[0]["days"][0][period] == before[0]["days"][0][period]
    else:
        assert after == before
    assert (await case.details(trip_id))["days"] == after[0]["days"]
    assert case.model.calls == [
        "trip_plan",
        "trip_edit_analysis",
        "trip_plan",
        "trip_plan",
    ]
    assert not case.model.replies
    if defect != "old_id":
        assert "Тестовый дневной парк" in case.context.preserved_names[-1]


@pytest.mark.parametrize("new_place_missing", [False, True])
@pytest.mark.parametrize(
    "required_reply", ["complete", "old_list_false", "old_list_true"]
)
async def test_named_addition_is_local_atomic_and_reopens_with_both_visits(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
    new_place_missing: bool,
    required_reply: str,
) -> None:
    """Реальная SQL-запись сохраняет парк и дополняет обязательные места из добавления."""

    case = lifecycle_case
    created = await case.create()
    trip_id = created["trip_id"]
    before = await stored_state(database_session, trip_id)
    if new_place_missing:
        case.context.missing_place_id = "second-museum"
    parsed = {
        **edit_analysis(change_rest=False),
        "interests_changed": False,
        "interests": None,
        "must_visit_places_changed": True,
        "must_visit_places": [REQUIRED_MUSEUM, "Тестовый музей"],
        "requires_regeneration": False,
        "regeneration_periods": [],
        "place_moves": [],
        "place_removals": [],
        "place_additions": [
            {
                "day": 1,
                "period": "afternoon",
                "place_name": "Тестовый музей",
                "activity_focus": "museum",
            }
        ],
    }
    if required_reply != "complete":
        parsed["must_visit_places"] = [REQUIRED_MUSEUM]
        parsed["must_visit_places_changed"] = required_reply == "old_list_true"
    case.model.replies.append(("trip_edit_analysis", parsed))
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": "Днём добавь Тестовый музей к прежнему парку. Остальные активности оставь.",
        },
    )
    assert response.status_code == (502 if new_place_missing else 200), response.text
    after = await stored_state(database_session, trip_id)
    if new_place_missing:
        assert after == before
    else:
        assert after[2] == before[2]
        assert after[1]["must_visit_places"] == [REQUIRED_MUSEUM, "Тестовый музей"]
        assert after[1]["interests"] == "Парки"
        assert (
            after[0]["days"][0]["afternoon"][0] == before[0]["days"][0]["afternoon"][0]
        )
        assert after[0]["days"][0]["afternoon"][1].startswith(
            "Тестовый музей: посетить музей."
        )
        for period in ("morning", "evening"):
            assert after[0]["days"][0][period] == before[0]["days"][0][period]
    assert (await case.details(trip_id))["days"] == after[0]["days"]
    assert case.model.calls == ["trip_plan", "trip_edit_analysis"]
    assert not case.model.replies


@pytest.mark.parametrize(
    "defect", [None, "removed_missing", "kept_missing", "empty_period"]
)
async def test_named_removal_after_addition_is_local_atomic_and_reopens(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
    defect: str | None,
) -> None:
    """Реальная SQL-правка удаляет одно посещение и его требование, сохраняя соседа."""

    case = lifecycle_case
    created = await case.create()
    trip_id = created["trip_id"]
    addition = {
        **edit_analysis(change_rest=False),
        "interests_changed": False,
        "interests": None,
        "must_visit_places_changed": True,
        "must_visit_places": [REQUIRED_MUSEUM, "Тестовый музей"],
        "requires_regeneration": False,
        "regeneration_periods": [],
        "place_additions": [
            {
                "day": 1,
                "period": "afternoon",
                "place_name": "Тестовый музей",
                "activity_focus": "museum",
            }
        ],
    }
    case.model.replies.append(("trip_edit_analysis", addition))
    added = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": "Днём добавь Тестовый музей к прежнему парку. Остальное оставь.",
        },
    )
    assert added.status_code == 200, added.text
    before = await stored_state(database_session, trip_id)
    if defect in {"removed_missing", "kept_missing"}:
        case.context.missing_place_id = (
            "second-museum" if defect == "removed_missing" else "day-park"
        )
    removal = {
        **edit_analysis(change_rest=False),
        "interests_changed": False,
        "interests": None,
        "must_visit_places_changed": True,
        "requires_regeneration": False,
        "regeneration_periods": [],
        "place_moves": [],
        "place_removals": [
            {
                "day": 1,
                "period": "afternoon",
                "place_name": "Тестовый музей",
            }
        ],
    }
    if defect == "empty_period":
        removal["must_visit_places"] = ["Тестовый музей"]
        removal["place_removals"] = [
            {"day": 1, "period": "morning", "place_name": REQUIRED_MUSEUM}
        ]
        instruction = f"Утром удали {REQUIRED_MUSEUM}. Остальное оставь."
    else:
        instruction = (
            "Днём удали Тестовый музей. Прежний парк и остальные активности оставь."
        )
    case.model.replies.append(("trip_edit_analysis", removal))
    lookup_count = len(case.context.calls)
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": instruction,
        },
    )
    failed = defect in {"kept_missing", "empty_period"}
    assert response.status_code == (502 if failed else 200), response.text
    after = await stored_state(database_session, trip_id)
    if failed:
        assert after == before
    else:
        assert after[2] == before[2]
        assert after[1]["must_visit_places"] == [REQUIRED_MUSEUM]
        assert after[1]["interests"] == "Парки"
        assert after[0]["days"][0]["afternoon"] == before[0]["days"][0]["afternoon"][:1]
        for period in ("morning", "evening"):
            assert after[0]["days"][0][period] == before[0]["days"][0][period]
    if defect == "empty_period":
        assert len(case.context.calls) == lookup_count
    else:
        assert "Тестовый музей" not in case.context.preserved_names[-1]
        assert "Тестовый дневной парк" in case.context.preserved_names[-1]
    assert (await case.details(trip_id))["days"] == after[0]["days"]
    assert case.model.calls == ["trip_plan", "trip_edit_analysis", "trip_edit_analysis"]
    assert not case.model.replies


@pytest.mark.parametrize("defect", [None, "missing", "closed", "far", "full"])
async def test_named_move_is_local_atomic_and_reopens_at_new_time(
    lifecycle_case: LifecycleCase,
    database_session: AsyncSession,
    defect: str | None,
) -> None:
    """Настоящий HTTP и PostgreSQL сохраняют оба конца переноса либо исходную запись."""

    case = lifecycle_case
    initial = grounded_plan()
    if defect == "full":
        initial["days"][0]["afternoon"].append(
            {
                "source_place_id": None,
                "place_name": None,
                "activity_focus": None,
                "description": "Неспешная пауза.",
            }
        )
    created = await case.create(plan=initial)
    trip_id = created["trip_id"]
    before = await stored_state(database_session, trip_id)
    if defect == "missing":
        case.context.missing_place_id = "required"
    elif defect in {"closed", "far"}:
        case.context.move_defect = defect
    parsed = {
        **edit_analysis(change_rest=False),
        "interests_changed": False,
        "interests": None,
        "requires_regeneration": False,
        "regeneration_periods": [],
        "place_moves": [
            {
                "place_name": REQUIRED_MUSEUM,
                "source_day": 1,
                "source_period": "morning",
                "target_day": 1,
                "target_period": "afternoon",
            }
        ],
    }
    case.model.replies.append(("trip_edit_analysis", parsed))
    response = await case.client.post(
        "/api/v1/trip-edit",
        json={
            "telegram_id": OWNER_ID,
            "trip_id": trip_id,
            "instruction": f"Перенеси {REQUIRED_MUSEUM} с утра на день. Все остальные активности оставь.",
        },
    )
    assert response.status_code == (200 if defect is None else 502), response.text
    after = await stored_state(database_session, trip_id)
    if defect is not None:
        assert after == before
    else:
        assert after[2] == before[2]
        assert after[1] == before[1]
        assert after[0]["days"][0]["morning"] == ["Свободное время."]
        assert (
            after[0]["days"][0]["afternoon"][0] == before[0]["days"][0]["afternoon"][0]
        )
        assert after[0]["days"][0]["afternoon"][1].startswith(
            f"{REQUIRED_MUSEUM}: посетить музей."
        )
        assert after[0]["days"][0]["evening"] == before[0]["days"][0]["evening"]
    assert REQUIRED_MUSEUM in case.context.preserved_names[-1]
    assert "Тестовый дневной парк" in case.context.preserved_names[-1]
    assert (await case.details(trip_id))["days"] == after[0]["days"]
    assert case.model.calls == ["trip_plan", "trip_edit_analysis"]
    assert not case.model.replies
