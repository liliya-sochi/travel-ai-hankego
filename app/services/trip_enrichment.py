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

PROVIDER_PLACE_LIMIT = 60
TRAVEL_CONTEXT_PLACE_LIMIT = 20

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


def _matches_requested_category(
    place: PlaceCandidate,
    requested_category: str,
) -> bool:
    """Проверяет принадлежность места к иерархической категории."""

    category_prefix = f"{requested_category}."

    return any(
        category == requested_category or category.startswith(category_prefix)
        for category in place.categories
    )


def _distance_sort_key(
    place: PlaceCandidate,
) -> tuple[bool, float, str, str]:
    """Формирует стабильный ключ сортировки по расстоянию."""

    distance = place.distance_meters

    return (
        distance is None,
        distance if distance is not None else 0.0,
        place.name.casefold(),
        place.source_place_id,
    )


def _quality_sort_key(
    place: PlaceCandidate,
) -> tuple[int, bool, int, bool, float, str, str]:
    """Формирует ключ полноты данных без рейтинга популярности."""

    distance_key = _distance_sort_key(place)

    return (
        -place.wiki_reference_count,
        "details.historic" not in place.available_details,
        -len(place.available_details),
        *distance_key,
    )


def _select_nearby_by_category(
    *,
    places: list[PlaceCandidate],
    requested_categories: list[str],
    limit: int,
) -> list[PlaceCandidate]:
    """Равномерно выбирает ближайшие места разных категорий."""

    ordered_places = sorted(
        places,
        key=_distance_sort_key,
    )
    selected: list[PlaceCandidate] = []
    selected_ids: set[str] = set()

    while len(selected) < limit:
        added_in_round = False

        for requested_category in requested_categories:
            candidate = next(
                (
                    place
                    for place in ordered_places
                    if place.source_place_id not in selected_ids
                    and _matches_requested_category(
                        place,
                        requested_category,
                    )
                ),
                None,
            )

            if candidate is None:
                continue

            selected.append(candidate)
            selected_ids.add(candidate.source_place_id)
            added_in_round = True

            if len(selected) == limit:
                break

        if not added_in_round:
            break

    for place in ordered_places:
        if len(selected) == limit:
            break

        if place.source_place_id in selected_ids:
            continue

        selected.append(place)
        selected_ids.add(place.source_place_id)

    return selected


def select_place_candidates(
    *,
    places: list[PlaceCandidate],
    requested_categories: list[str],
    limit: int = TRAVEL_CONTEXT_PLACE_LIMIT,
) -> list[PlaceCandidate]:
    """Выбирает разнообразный и качественный shortlist мест."""

    if not requested_categories:
        raise ValueError("Для ranking нужна хотя бы одна категория.")

    if not 1 <= limit <= TRAVEL_CONTEXT_PLACE_LIMIT:
        raise ValueError("Размер shortlist должен быть от 1 до 20.")

    unique_places: list[PlaceCandidate] = []
    seen_place_ids: set[str] = set()

    for place in places:
        if place.source_place_id in seen_place_ids:
            continue

        seen_place_ids.add(place.source_place_id)
        unique_places.append(place)

    nearby_limit = min(
        (limit + 1) // 2,
        len(unique_places),
    )

    selected = _select_nearby_by_category(
        places=unique_places,
        requested_categories=requested_categories,
        limit=nearby_limit,
    )
    selected_ids = {place.source_place_id for place in selected}

    quality_ordered_places = sorted(
        unique_places,
        key=_quality_sort_key,
    )

    for place in quality_ordered_places:
        if len(selected) == limit:
            break

        if place.source_place_id in selected_ids:
            continue

        selected.append(place)
        selected_ids.add(place.source_place_id)

    return selected


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

            place_candidates = await self._places_provider.search_places(
                location=location,
                categories=categories,
                limit=PROVIDER_PLACE_LIMIT,
            )

        except GeoapifyServiceError as error:
            raise TripEnrichmentError(str(error)) from error

        if not place_candidates:
            raise TripEnrichmentError(
                "Не удалось найти актуальные места для указанного направления."
            )

        places = select_place_candidates(
            places=place_candidates,
            requested_categories=categories,
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
