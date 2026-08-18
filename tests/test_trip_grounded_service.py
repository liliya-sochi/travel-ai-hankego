"""Unit-тест orchestration grounded-генерации."""

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
    TripPlanResponse,
    TripPreferences,
)
from app.services.trip import TripService


class FakeSession:
    """Минимальная тестовая SQLAlchemy-сессия."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        """Фиксирует факт commit."""

        self.committed = True

    async def rollback(self) -> None:
        """Фиксирует факт rollback."""

        self.rolled_back = True


class FakeUserRepository:
    """Возвращает тестового пользователя."""

    async def upsert_telegram_user(
        self,
        **_: Any,
    ) -> SimpleNamespace:
        """Имитирует создание пользователя."""

        return SimpleNamespace(id=11)


class FakeTripRepository:
    """Сохраняет параметры тестового маршрута."""

    def __init__(self) -> None:
        self.plan_data: dict[str, object] | None = None

    async def create_trip(
        self,
        **arguments: Any,
    ) -> SimpleNamespace:
        """Имитирует сохранение маршрута."""

        self.plan_data = arguments["plan_data"]

        return SimpleNamespace(
            id=7,
            created_at=datetime(
                2026,
                8,
                18,
                8,
                0,
                tzinfo=UTC,
            ),
        )


class FakeEnrichmentService:
    """Возвращает фиксированный TravelContext."""

    def __init__(self) -> None:
        self.received_preferences: TripPreferences | None = None
        self.context = TravelContext(
            location=DestinationLocation(
                formatted_name="Стамбул, Турция",
                latitude=41.0082,
                longitude=28.9784,
                source_place_id="istanbul-id",
            ),
            requested_categories=[
                "tourism.sights",
            ],
            places=[
                PlaceCandidate(
                    name="Айя-София",
                    formatted_address=("Султанахмет, Стамбул"),
                    latitude=41.0086,
                    longitude=28.9802,
                    categories=["tourism.sights"],
                    source_place_id="hagia-sophia-id",
                )
            ],
            fetched_at=datetime.now(UTC),
        )

    async def enrich(
        self,
        preferences: TripPreferences,
    ) -> TravelContext:
        """Возвращает контекст без внешнего API."""

        self.received_preferences = preferences
        return self.context


@pytest.mark.asyncio
async def test_enriches_before_generation_and_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет полный порядок orchestration."""

    captured_generation: dict[str, object] = {}

    async def fake_generate_trip_plan(
        *,
        preferences: TripPreferences,
        travel_context: TravelContext,
    ) -> TripPlanResponse:
        captured_generation["preferences"] = preferences
        captured_generation["context"] = travel_context

        return TripPlanResponse(
            destination="Стамбул",
            duration_days=1,
            summary="Проверенный маршрут.",
            days=[
                {
                    "day": 1,
                    "title": "Исторический центр",
                    "morning": ["Айя-София"],
                    "afternoon": ["Прогулка"],
                    "evening": ["Отдых"],
                }
            ],
            practical_tips=["Проверить актуальные часы работы."],
        )

    monkeypatch.setattr(
        trip_service_module,
        "generate_trip_plan",
        fake_generate_trip_plan,
    )

    session = FakeSession()
    enrichment_service = FakeEnrichmentService()
    trip_repository = FakeTripRepository()

    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = trip_repository

    preferences = TripPreferences(
        destination="Стамбул",
        duration_days=1,
        interests="История",
    )

    result = await service.create_trip_plan(
        telegram_id=9000000001,
        first_name="Liliya",
        preferences=preferences,
        enrichment_service=enrichment_service,
    )

    assert enrichment_service.received_preferences == preferences
    assert captured_generation["preferences"] == preferences
    assert captured_generation["context"] == enrichment_service.context
    assert session.committed is True
    assert session.rolled_back is False
    assert trip_repository.plan_data is not None
    assert result.trip_id == 7
    assert result.destination == "Стамбул"
