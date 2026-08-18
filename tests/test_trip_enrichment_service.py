"""Unit-тесты сервиса обогащения поездки."""

import pytest

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
)
from app.schemas.trip import TripPreferences
from app.services.geoapify import GeoapifyServiceError
from app.services.trip_enrichment import (
    TripEnrichmentError,
    TripEnrichmentService,
    select_place_categories,
)


class FakePlacesProvider:
    """Управляемый источник мест для unit-тестов."""

    def __init__(
        self,
        *,
        places: list[PlaceCandidate] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.places = places or []
        self.error = error
        self.received_destination: str | None = None
        self.received_categories: list[str] | None = None

    async def geocode_destination(
        self,
        destination: str,
    ) -> DestinationLocation:
        """Возвращает тестовое положение направления."""

        self.received_destination = destination

        if self.error is not None:
            raise self.error

        return DestinationLocation(
            formatted_name="Стамбул, Турция",
            latitude=41.0082,
            longitude=28.9784,
            source_place_id="istanbul-place-id",
        )

    async def search_places(
        self,
        *,
        location: DestinationLocation,
        categories: list[str],
        limit: int = 20,
        radius_meters: int = 15_000,
    ) -> list[PlaceCandidate]:
        """Возвращает подготовленные тестовые места."""

        self.received_categories = categories

        assert location.source_place_id == "istanbul-place-id"
        assert limit == 20
        assert radius_meters == 15_000

        return self.places


def build_place() -> PlaceCandidate:
    """Создаёт корректное тестовое место."""

    return PlaceCandidate(
        name="Айя-София",
        formatted_address="Султанахмет, Стамбул",
        latitude=41.0086,
        longitude=28.9802,
        categories=["tourism.sights"],
        source_place_id="hagia-sophia-id",
    )


def test_default_categories_do_not_require_interests() -> None:
    """Проверяет базовые категории маршрута."""

    categories = select_place_categories(None)

    assert categories == [
        "tourism.sights",
        "entertainment.museum",
    ]


def test_selects_categories_from_interests() -> None:
    """Проверяет детерминированный подбор категорий."""

    categories = select_place_categories("Местная кухня, природа и ночная жизнь")

    assert categories == [
        "tourism.sights",
        "entertainment.museum",
        "catering.restaurant",
        "leisure.park",
        "entertainment",
    ]


@pytest.mark.asyncio
async def test_enriches_trip_preferences() -> None:
    """Проверяет создание полного TravelContext."""

    provider = FakePlacesProvider(
        places=[build_place()],
    )
    service = TripEnrichmentService(provider)

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=3,
            interests="История и местная кухня",
        )
    )

    assert provider.received_destination == "Стамбул"
    assert provider.received_categories == [
        "tourism.sights",
        "entertainment.museum",
        "catering.restaurant",
    ]
    assert context.location.formatted_name == "Стамбул, Турция"
    assert context.places == [build_place()]
    assert context.source == "geoapify"
    assert context.fetched_at.tzinfo is not None
    assert "Geoapify" in context.attribution
    assert "OpenStreetMap" in context.attribution


@pytest.mark.asyncio
async def test_rejects_empty_places_result() -> None:
    """Проверяет ошибку при отсутствии реальных мест."""

    service = TripEnrichmentService(
        FakePlacesProvider(),
    )

    with pytest.raises(
        TripEnrichmentError,
        match="Не удалось найти актуальные места",
    ):
        await service.enrich(
            TripPreferences(
                destination="Стамбул",
                duration_days=3,
            )
        )


@pytest.mark.asyncio
async def test_converts_provider_error() -> None:
    """Проверяет безопасное преобразование ошибки Geoapify."""

    service = TripEnrichmentService(
        FakePlacesProvider(
            error=GeoapifyServiceError("Сервис туристических данных недоступен."),
        )
    )

    with pytest.raises(
        TripEnrichmentError,
        match="Сервис туристических данных недоступен",
    ):
        await service.enrich(
            TripPreferences(
                destination="Стамбул",
                duration_days=3,
            )
        )
