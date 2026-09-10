"""Unit-тесты orchestration редактирования сохранённого маршрута."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.trip as trip_service_module
from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    TravelContext,
)
from app.schemas.trip import (
    TripEditAnalysis,
    TripPlanResponse,
    TripPreferences,
)
from app.services.trip import (
    TripEditUnavailableError,
    TripEditUnsupportedError,
    TripService,
)


def build_plan() -> TripPlanResponse:
    """Создаёт текущую или обновлённую версию маршрута."""

    return TripPlanResponse(
        destination="Стамбул",
        duration_days=1,
        summary="Маршрут по Стамбулу.",
        days=[
            {
                "day": 1,
                "title": "Исторический центр",
                "morning": ["Прогулка"],
                "afternoon": ["Музей"],
                "evening": ["Отдых"],
            }
        ],
        practical_tips=["Проверяйте расписание."],
    )


class FakeSession:
    """Считает завершения читающей и записывающей транзакций."""

    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class FakeUserRepository:
    """Возвращает владельца маршрута."""

    async def get_by_telegram_id(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(id=11)


class FakeEnrichmentService:
    """Возвращает свежий фиксированный контекст."""

    def __init__(self) -> None:
        self.received_preferences: TripPreferences | None = None
        self.context = TravelContext(
            location=DestinationLocation(
                formatted_name="Стамбул, Турция",
                latitude=41.0082,
                longitude=28.9784,
                source_place_id="istanbul-id",
            ),
            requested_categories=["tourism.sights"],
            places=[
                PlaceCandidate(
                    name="Айя-София",
                    formatted_address="Султанахмет, Стамбул",
                    latitude=41.0086,
                    longitude=28.9802,
                    categories=["tourism.sights"],
                    source_place_id="hagia-sophia-id",
                )
            ],
            fetched_at=datetime.now(UTC),
        )

    async def enrich(self, preferences: TripPreferences) -> TravelContext:
        self.received_preferences = preferences
        return self.context


class FakeTripRepository:
    """Хранит исходные и обновлённые данные в памяти."""

    def __init__(self, *, preferences_data: dict[str, object] | None) -> None:
        self.trip = SimpleNamespace(
            id=7,
            user_id=11,
            plan_data=build_plan().model_dump(mode="json"),
            preferences_data=preferences_data,
            created_at=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
        )
        self.update_arguments: dict[str, object] | None = None

    async def get_by_id_and_user_id(self, **_: Any) -> SimpleNamespace:
        return self.trip

    async def update_by_id_and_user_id(
        self,
        **arguments: Any,
    ) -> SimpleNamespace:
        self.update_arguments = arguments
        return self.trip


@pytest.mark.asyncio
async def test_edit_rebuilds_context_and_saves_full_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Изменяет preferences, обогащает их заново и делает один commit."""

    original_preferences = TripPreferences(
        destination="Стамбул",
        duration_days=1,
        interests="Архитектура",
    )
    analysis = TripEditAnalysis(
        supported=True,
        interests_changed=True,
        interests="Архитектура и музеи",
        must_visit_places_changed=True,
        must_visit_places=["Айя-София"],
    )
    captured: dict[str, object] = {}

    async def fake_analyze_trip_edit(**arguments: Any) -> TripEditAnalysis:
        captured["analysis_arguments"] = arguments
        return analysis

    async def fake_generate_edited_trip_plan(
        *,
        preferences: TripPreferences,
        travel_context: TravelContext,
        current_plan: TripPlanResponse,
        instruction: str,
    ) -> TripPlanResponse:
        captured["generation_preferences"] = preferences
        captured["generation_context"] = travel_context
        captured["current_plan"] = current_plan
        captured["instruction"] = instruction
        return build_plan()

    monkeypatch.setattr(
        trip_service_module,
        "analyze_trip_edit",
        fake_analyze_trip_edit,
    )
    monkeypatch.setattr(
        trip_service_module,
        "generate_edited_trip_plan",
        fake_generate_edited_trip_plan,
    )

    session = FakeSession()
    repository = FakeTripRepository(
        preferences_data=original_preferences.model_dump(mode="json"),
    )
    enrichment_service = FakeEnrichmentService()
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository

    result = await service.edit_trip_plan(
        telegram_id=9000000001,
        trip_id=7,
        instruction="Добавь Айя-Софию",
        enrichment_service=enrichment_service,
    )

    expected_preferences = TripPreferences(
        destination="Стамбул",
        duration_days=1,
        interests="Архитектура и музеи",
        must_visit_places=["Айя-София"],
    )
    assert enrichment_service.received_preferences == expected_preferences
    assert captured["generation_preferences"] == expected_preferences
    assert captured["instruction"] == "Добавь Айя-Софию"
    assert repository.update_arguments is not None
    assert repository.update_arguments["preferences_data"] == (
        expected_preferences.model_dump(mode="json")
    )
    assert session.rollback_count == 1
    assert session.commit_count == 1
    assert result.trip_id == 7
    assert result.updated is True


@pytest.mark.asyncio
async def test_edit_rejects_legacy_trip_before_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не угадывает preferences для старого сохранённого маршрута."""

    async def unexpected_analysis(**_: Any) -> None:
        pytest.fail("LLM не должна вызываться для старого маршрута")

    monkeypatch.setattr(
        trip_service_module,
        "analyze_trip_edit",
        unexpected_analysis,
    )
    session = FakeSession()
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = FakeTripRepository(preferences_data=None)

    with pytest.raises(TripEditUnavailableError, match="создан до"):
        await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction="Замени вечер",
            enrichment_service=FakeEnrichmentService(),
        )

    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_edit_rejects_destination_or_duration_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Смена поездки останавливается до enrichment и генерации."""

    async def fake_analyze_trip_edit(**_: Any) -> TripEditAnalysis:
        return TripEditAnalysis(
            supported=False,
            interests_changed=False,
            interests=None,
            must_visit_places_changed=False,
            must_visit_places=[],
        )

    monkeypatch.setattr(
        trip_service_module,
        "analyze_trip_edit",
        fake_analyze_trip_edit,
    )
    enrichment_service = FakeEnrichmentService()
    preferences = TripPreferences(destination="Стамбул", duration_days=1)
    service = TripService(FakeSession())  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = FakeTripRepository(
        preferences_data=preferences.model_dump(mode="json"),
    )

    with pytest.raises(TripEditUnsupportedError, match="количество дней"):
        await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction="Сделай три дня в Риме",
            enrichment_service=enrichment_service,
        )

    assert enrichment_service.received_preferences is None
