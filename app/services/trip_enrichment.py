"""
Обогащение параметров поездки актуальными туристическими данными.

Сервис находится между пользовательскими предпочтениями и LLM:
TripPreferences -> Geoapify -> TravelContext -> LLM.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    PlaceDetails,
    TravelContext,
)
from app.schemas.trip import TripPreferences
from app.services.geoapify import GeoapifyServiceError
from app.services.google_places import (
    GooglePlacesBudgetUnavailableError,
    GooglePlacesServiceError,
)
from app.services.place_geography import (
    calculate_distance_meters,
    calculate_place_grid_cell,
)

logger = logging.getLogger(__name__)

DEFAULT_PLACE_CATEGORIES = [
    "tourism.sights",
    "entertainment.museum",
]

PROVIDER_PLACE_LIMIT = 120
TRAVEL_CONTEXT_PLACE_LIMIT = 20
PLACE_DETAILS_LIMIT = 5

SCHEDULE_SENSITIVE_CATEGORY_PREFIXES = (
    "entertainment.museum",
    "building.tourism",
    "tourism.sights.castle",
    "tourism.sights.memorial.necropolis",
)

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

    async def get_place_details(
        self,
        source_place_id: str,
    ) -> PlaceDetails:
        """Получает дополнительные сведения о месте."""

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


class OpeningHoursFallbackProvider(Protocol):
    """Контракт резервного источника данных места."""

    async def enrich_place(
        self,
        place: PlaceCandidate,
    ) -> PlaceCandidate:
        """Возвращает исходное или дополненное место."""

        ...


class OpeningHoursBudget(Protocol):
    """Контракт глобального ограничителя платных запросов."""

    async def try_acquire(self) -> bool:
        """Резервирует один запрос, если месячный лимит не исчерпан."""

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
    location: DestinationLocation,
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
        max(1, (limit + 3) // 4),
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

    quality_limit = min(
        limit,
        nearby_limit + max(1, limit // 2),
    )

    for place in quality_ordered_places:
        if len(selected) == quality_limit:
            break

        if place.source_place_id in selected_ids:
            continue

        selected.append(place)
        selected_ids.add(place.source_place_id)

    selected_cells = {
        calculate_place_grid_cell(
            place=place,
            location=location,
        )
        for place in selected
    }

    for place in quality_ordered_places:
        if len(selected) == limit:
            break

        if place.source_place_id in selected_ids:
            continue

        place_cell = calculate_place_grid_cell(
            place=place,
            location=location,
        )

        if place_cell in selected_cells:
            continue

        selected.append(place)
        selected_ids.add(place.source_place_id)
        selected_cells.add(place_cell)

    for place in quality_ordered_places:
        if len(selected) == limit:
            break

        if place.source_place_id in selected_ids:
            continue

        selected.append(place)
        selected_ids.add(place.source_place_id)

    return selected


def _place_details_sort_key(
    place: PlaceCandidate,
) -> tuple[bool, int, int, bool, float, str, str]:
    """Ставит выше места с контактными и справочными данными."""

    distance_key = _distance_sort_key(place)

    return (
        "details.contact" not in place.available_details,
        -place.wiki_reference_count,
        -len(place.available_details),
        *distance_key,
    )


def select_place_details_candidates(
    *,
    places: list[PlaceCandidate],
    limit: int = PLACE_DETAILS_LIMIT,
) -> list[PlaceCandidate]:
    """Выбирает места, для которых вероятнее получить полезные детали."""

    if not 1 <= limit <= PLACE_DETAILS_LIMIT:
        raise ValueError("Количество запросов Place Details должно быть от 1 до 5.")

    documented_places = [
        place
        for place in places
        if any(
            detail == "details" or detail.startswith("details.")
            for detail in place.available_details
        )
    ]

    return sorted(
        documented_places,
        key=_place_details_sort_key,
    )[:limit]


def requires_opening_hours_fallback(place: PlaceCandidate) -> bool:
    """Выбирает режимные объекты без расписания Geoapify."""

    if place.opening_hours is not None:
        return False

    for category in place.categories:
        if any(
            category == prefix or category.startswith(f"{prefix}.")
            for prefix in SCHEDULE_SENSITIVE_CATEGORY_PREFIXES
        ):
            return True

    return (
        "fee" in place.categories
        and "tourism.sights.archaeological_site" in place.categories
    )


class TripEnrichmentService:
    """Собирает проверенный контекст для генерации маршрута."""

    def __init__(
        self,
        places_provider: PlacesDataProvider,
        travel_context_cache: TravelContextCache | None = None,
        opening_hours_fallback_provider: OpeningHoursFallbackProvider | None = None,
        opening_hours_budget: OpeningHoursBudget | None = None,
        opening_hours_fallback_limit: int = 2,
    ) -> None:
        self._places_provider = places_provider
        self._travel_context_cache = travel_context_cache
        self._opening_hours_fallback_provider = opening_hours_fallback_provider
        self._opening_hours_budget = opening_hours_budget

        if opening_hours_fallback_limit <= 0:
            raise ValueError("Opening hours fallback limit must be positive.")

        self._opening_hours_fallback_limit = opening_hours_fallback_limit

    async def _get_place_details_or_none(
        self,
        place: PlaceCandidate,
    ) -> PlaceDetails | None:
        """Получает детали одного места без остановки всего маршрута."""

        try:
            return await self._places_provider.get_place_details(place.source_place_id)

        except GeoapifyServiceError:
            # Дополнительные сведения являются улучшением.
            # Ошибка одного details-запроса не должна отменять маршрут.
            logger.warning(
                "Failed to load Geoapify place details",
                extra={
                    "source_place_id": place.source_place_id,
                },
            )
            return None

    async def _enrich_place_details(
        self,
        places: list[PlaceCandidate],
    ) -> list[PlaceCandidate]:
        """Параллельно добавляет доступные сайты и часы работы."""

        detail_candidates = select_place_details_candidates(
            places=places,
        )

        if not detail_candidates:
            return places

        detail_results = await asyncio.gather(
            *(self._get_place_details_or_none(place) for place in detail_candidates)
        )

        details_by_place_id = {
            details.source_place_id: details
            for details in detail_results
            if details is not None
        }

        enriched_places: list[PlaceCandidate] = []

        for place in places:
            details = details_by_place_id.get(place.source_place_id)

            if details is None:
                enriched_places.append(place)
                continue

            enriched_places.append(
                PlaceCandidate.model_validate(
                    {
                        **place.model_dump(),
                        "website": details.website,
                        "opening_hours": details.opening_hours,
                        "opening_hours_source": "geoapify",
                    }
                )
            )

        return enriched_places

    async def _enrich_missing_opening_hours(
        self,
        context: TravelContext,
    ) -> TravelContext:
        """Добавляет Google-данные без их сохранения в кеш."""

        provider = self._opening_hours_fallback_provider
        budget = self._opening_hours_budget

        if provider is None or budget is None:
            return context

        detail_candidates = select_place_details_candidates(
            places=context.places,
        )
        fallback_candidates = [
            place
            for place in detail_candidates
            if requires_opening_hours_fallback(place)
        ][: self._opening_hours_fallback_limit]

        allowed_candidates: list[PlaceCandidate] = []

        for place in fallback_candidates:
            try:
                is_allowed = await budget.try_acquire()
            except GooglePlacesBudgetUnavailableError:
                logger.warning("Google Places budget limiter is unavailable")
                break

            if not is_allowed:
                logger.warning("Google Places monthly lookup limit reached")
                break

            allowed_candidates.append(place)

        fallback_results = await asyncio.gather(
            *(provider.enrich_place(place) for place in allowed_candidates),
            return_exceptions=True,
        )
        enriched_by_place_id: dict[str, PlaceCandidate] = {}

        for place, result in zip(
            allowed_candidates,
            fallback_results,
            strict=True,
        ):
            if isinstance(result, GooglePlacesServiceError):
                logger.warning(
                    "Failed to load Google Places enrichment",
                    extra={"source_place_id": place.source_place_id},
                )
                continue

            if isinstance(result, BaseException):
                raise result

            if result.source_place_id != place.source_place_id:
                raise ValueError("Google enrichment changed source place ID.")

            if result.location_source == "google":
                result = result.model_copy(
                    update={
                        "distance_meters": (
                            calculate_distance_meters(
                                first_latitude=(context.location.latitude),
                                first_longitude=(context.location.longitude),
                                second_latitude=result.latitude,
                                second_longitude=result.longitude,
                            )
                        )
                    }
                )

            if result != place:
                enriched_by_place_id[place.source_place_id] = result

        if not enriched_by_place_id:
            return context

        enriched_places = [
            enriched_by_place_id.get(
                place.source_place_id,
                place,
            )
            for place in context.places
        ]

        return context.model_copy(update={"places": enriched_places})

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
                return await self._enrich_missing_opening_hours(cached_context)

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
            location=location,
            requested_categories=categories,
        )

        places = await self._enrich_place_details(places)

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

        return await self._enrich_missing_opening_hours(context)
