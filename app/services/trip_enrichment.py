"""
Обогащение параметров поездки актуальными туристическими данными.

Сервис находится между пользовательскими предпочтениями и LLM:
TripPreferences -> Geoapify -> TravelContext -> LLM.
"""

from datetime import UTC, datetime
from typing import Protocol

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    TravelContext,
)
from app.schemas.trip import TripPreferences
from app.services.geoapify import GeoapifyServiceError

DEFAULT_PLACE_CATEGORIES = [
    "tourism.sights",
    "entertainment.museum",
]

INTEREST_CATEGORY_RULES: tuple[
    tuple[tuple[str, ...], str],
    ...,
] = (
    (
        (
            "еда",
            "кухн",
            "гастроном",
            "ресторан",
            "food",
            "cuisine",
            "restaurant",
        ),
        "catering.restaurant",
    ),
    (
        (
            "природ",
            "парк",
            "сад",
            "nature",
            "park",
            "garden",
        ),
        "leisure.park",
    ),
    (
        (
            "развлеч",
            "ночн",
            "концерт",
            "театр",
            "entertainment",
            "nightlife",
            "concert",
            "theatre",
        ),
        "entertainment",
    ),
)


class PlacesDataProvider(Protocol):
    """Контракт источника географических данных."""

    async def geocode_destination(
        self,
        destination: str,
    ) -> DestinationLocation:
        """Определяет координаты направления."""

        ...

    async def search_places(
        self,
        *,
        location: DestinationLocation,
        categories: list[str],
        limit: int = 20,
        radius_meters: int = 15_000,
    ) -> list[PlaceCandidate]:
        """Ищет места по категориям."""

        ...


class TravelContextCache(Protocol):
    """Контракт кеша проверенного туристического контекста."""

    async def get(
        self,
        *,
        destination: str,
        categories: list[str],
    ) -> TravelContext | None:
        """Возвращает контекст или сообщает о промахе кеша."""

        ...

    async def set(
        self,
        *,
        destination: str,
        categories: list[str],
        context: TravelContext,
    ) -> None:
        """Сохраняет проверенный контекст."""

        ...


class TripEnrichmentError(Exception):
    """Безопасная ошибка обогащения маршрута."""


def select_place_categories(
    interests: str | None,
) -> list[str]:
    """
    Детерминированно выбирает категории по интересам.

    Базовые достопримечательности и музеи используются всегда.
    Дополнительные категории добавляются только при совпадении
    с явно указанными интересами пользователя.
    """

    categories = DEFAULT_PLACE_CATEGORIES.copy()

    if interests is None:
        return categories

    normalized_interests = interests.casefold()

    for keywords, category in INTEREST_CATEGORY_RULES:
        if any(keyword in normalized_interests for keyword in keywords):
            categories.append(category)

    return categories


class TripEnrichmentService:
    """Собирает проверенный контекст для генерации маршрута."""

    def __init__(
        self,
        places_provider: PlacesDataProvider,
        travel_context_cache: TravelContextCache | None = None,
    ) -> None:
        self._places_provider = places_provider
        self._travel_context_cache = travel_context_cache

    async def enrich(
        self,
        preferences: TripPreferences,
    ) -> TravelContext:
        """Обогащает параметры поездки актуальными местами."""

        categories = select_place_categories(preferences.interests)

        if self._travel_context_cache is not None:
            cached_context = await self._travel_context_cache.get(
                destination=preferences.destination,
                categories=categories,
            )

            if cached_context is not None:
                return cached_context

        try:
            location = await self._places_provider.geocode_destination(
                preferences.destination
            )

            places = await self._places_provider.search_places(
                location=location,
                categories=categories,
                limit=20,
            )

        except GeoapifyServiceError as error:
            raise TripEnrichmentError(str(error)) from error

        if not places:
            raise TripEnrichmentError(
                "Не удалось найти актуальные места для указанного направления."
            )

        context = TravelContext(
            location=location,
            requested_categories=categories,
            places=places,
            fetched_at=datetime.now(UTC),
        )

        if self._travel_context_cache is not None:
            await self._travel_context_cache.set(
                destination=preferences.destination,
                categories=categories,
                context=context,
            )

        return context
