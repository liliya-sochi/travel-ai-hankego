"""Unit-тесты сервиса обогащения поездки."""

from datetime import UTC, datetime

import pytest

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    PlaceDetails,
    TravelContext,
)
from app.schemas.trip import TripPreferences
from app.services.geoapify import GeoapifyServiceError
from app.services.google_places import GooglePlacesServiceError
from app.services.trip_enrichment import (
    TripEnrichmentError,
    TripEnrichmentService,
    requires_opening_hours_fallback,
    select_place_candidates,
    select_place_categories,
)


class FakeOpeningHoursFallbackProvider:
    """Управляемый резервный источник данных места."""

    def __init__(
        self,
        *,
        hours: dict[str, str | None] | None = None,
        updates: dict[str, dict[str, object]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.hours = hours or {}
        self.updates = updates or {}
        self.error = error
        self.received_place_ids: list[str] = []

    async def enrich_place(
        self,
        place: PlaceCandidate,
    ) -> PlaceCandidate:
        """Возвращает подготовленное дополнение места."""

        self.received_place_ids.append(place.source_place_id)

        if self.error is not None:
            raise self.error

        place_updates = dict(self.updates.get(place.source_place_id, {}))
        opening_hours = self.hours.get(place.source_place_id)

        if opening_hours is not None:
            place_updates.update(
                {
                    "opening_hours": opening_hours,
                    "opening_hours_source": "google",
                }
            )

        if not place_updates:
            return place

        return place.model_copy(update=place_updates)


class FakeOpeningHoursBudget:
    """Считает попытки резервирования Google-запросов."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def try_acquire(self) -> bool:
        """Возвращает заданное решение."""

        self.calls += 1
        return self.allowed


class FakePlacesProvider:
    """Управляемый источник мест для unit-тестов."""

    def __init__(
        self,
        *,
        places: list[PlaceCandidate] | None = None,
        details: dict[str, PlaceDetails] | None = None,
        error: Exception | None = None,
        details_error: Exception | None = None,
    ) -> None:
        self.places = places or []
        self.details = details or {}
        self.error = error
        self.details_error = details_error
        self.received_destination: str | None = None
        self.received_categories: list[str] | None = None
        self.received_detail_place_ids: list[str] = []

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
        assert limit == 120
        assert radius_meters == 15_000

        return self.places

    async def get_place_details(
        self,
        source_place_id: str,
    ) -> PlaceDetails:
        """Возвращает подготовленные дополнительные сведения."""

        self.received_detail_place_ids.append(source_place_id)

        if self.details_error is not None:
            raise self.details_error

        return self.details[source_place_id]


class FakeTravelContextCache:
    """Управляемый кеш туристического контекста."""

    def __init__(
        self,
        cached_context: TravelContext | None = None,
    ) -> None:
        self.cached_context = cached_context
        self.get_destination: str | None = None
        self.get_categories: list[str] | None = None
        self.set_destination: str | None = None
        self.set_categories: list[str] | None = None
        self.saved_context: TravelContext | None = None

    async def get(
        self,
        *,
        destination: str,
        categories: list[str],
    ) -> TravelContext | None:
        """Возвращает подготовленный результат кеша."""

        self.get_destination = destination
        self.get_categories = categories

        return self.cached_context

    async def set(
        self,
        *,
        destination: str,
        categories: list[str],
        context: TravelContext,
    ) -> None:
        """Запоминает контекст, переданный для сохранения."""

        self.set_destination = destination
        self.set_categories = categories
        self.saved_context = context


def build_place(
    *,
    name: str = "Айя-София",
    source_place_id: str = "hagia-sophia-id",
    categories: list[str] | None = None,
    latitude: float = 41.0086,
    longitude: float = 28.9802,
    distance_meters: float | None = None,
    available_details: list[str] | None = None,
    wiki_reference_count: int = 0,
) -> PlaceCandidate:
    """Создаёт корректное тестовое место."""

    return PlaceCandidate(
        name=name,
        formatted_address="Султанахмет, Стамбул",
        latitude=latitude,
        longitude=longitude,
        categories=(categories if categories is not None else ["tourism.sights"]),
        distance_meters=distance_meters,
        available_details=(available_details if available_details is not None else []),
        wiki_reference_count=wiki_reference_count,
        source_place_id=source_place_id,
    )


def build_location() -> DestinationLocation:
    """Создаёт тестовый центр направления."""

    return DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )


def build_context() -> TravelContext:
    """Создаёт готовый кешированный TravelContext."""

    return TravelContext(
        location=build_location(),
        requested_categories=[
            "tourism.sights",
            "entertainment.museum",
        ],
        places=[build_place()],
        fetched_at=datetime(
            2026,
            8,
            20,
            12,
            0,
            tzinfo=UTC,
        ),
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


def test_selects_nearby_categories_and_documented_places() -> None:
    """Проверяет баланс близости, категорий и полноты данных."""

    places = [
        build_place(
            name="Ближайшая достопримечательность",
            source_place_id="nearby-sight",
            categories=["tourism.sights"],
            distance_meters=10.0,
        ),
        build_place(
            name="Ближайший музей",
            source_place_id="nearby-museum",
            categories=["entertainment.museum"],
            distance_meters=20.0,
        ),
        build_place(
            name="Ближайший ресторан",
            source_place_id="nearby-restaurant",
            categories=["catering.restaurant"],
            distance_meters=30.0,
        ),
        build_place(
            name="Вторая достопримечательность",
            source_place_id="second-sight",
            categories=["tourism.sights"],
            distance_meters=40.0,
        ),
        build_place(
            name="Второй музей",
            source_place_id="second-museum",
            categories=["entertainment.museum"],
            distance_meters=50.0,
        ),
        build_place(
            name="Второй ресторан",
            source_place_id="second-restaurant",
            categories=["catering.restaurant"],
            distance_meters=60.0,
        ),
        build_place(
            name="Хорошо документированное место",
            source_place_id="documented-place",
            categories=["tourism.sights"],
            distance_meters=900.0,
            available_details=[
                "details",
                "details.historic",
                "details.wiki_and_media",
            ],
            wiki_reference_count=4,
        ),
    ]

    selected = select_place_candidates(
        places=places,
        location=build_location(),
        requested_categories=[
            "tourism.sights",
            "entertainment.museum",
            "catering.restaurant",
        ],
        limit=6,
    )

    selected_ids = [place.source_place_id for place in selected]

    assert selected_ids[:3] == [
        "nearby-sight",
        "nearby-museum",
        "documented-place",
    ]
    assert "documented-place" in selected_ids
    assert len(selected) == 6


def test_deduplicates_places_by_source_place_id() -> None:
    """Проверяет удаление повторяющихся provider ID."""

    places = [
        build_place(
            name="Первый объект",
            source_place_id="duplicate-id",
            distance_meters=10.0,
        ),
        build_place(
            name="Повторный объект",
            source_place_id="duplicate-id",
            distance_meters=20.0,
            wiki_reference_count=4,
        ),
        build_place(
            name="Другой объект",
            source_place_id="unique-id",
            distance_meters=30.0,
        ),
    ]

    selected = select_place_candidates(
        places=places,
        location=build_location(),
        requested_categories=["tourism.sights"],
    )

    assert [place.source_place_id for place in selected] == [
        "duplicate-id",
        "unique-id",
    ]
    assert selected[0].name == "Первый объект"


def test_balances_quality_and_geographic_cells() -> None:
    """Проверяет баланс качества и географического разнообразия."""

    places = [
        build_place(
            name=f"Центральное место {index}",
            source_place_id=f"central-{index}",
            distance_meters=float(index * 10),
        )
        for index in range(1, 4)
    ]
    places.extend(
        [
            build_place(
                name="Документированное место в центре",
                source_place_id="documented-center",
                distance_meters=100.0,
                wiki_reference_count=4,
            ),
            build_place(
                name="Северный район",
                source_place_id="north",
                latitude=41.0382,
                longitude=28.9784,
                distance_meters=3_300.0,
                wiki_reference_count=1,
            ),
            build_place(
                name="Восточный район",
                source_place_id="east",
                latitude=41.0082,
                longitude=29.0184,
                distance_meters=3_350.0,
                wiki_reference_count=1,
            ),
            build_place(
                name="Западный район",
                source_place_id="west",
                latitude=41.0082,
                longitude=28.9384,
                distance_meters=3_350.0,
                wiki_reference_count=1,
            ),
        ]
    )

    selected = select_place_candidates(
        places=places,
        location=build_location(),
        requested_categories=["tourism.sights"],
        limit=6,
    )

    selected_ids = {place.source_place_id for place in selected}

    assert selected_ids == {
        "central-1",
        "central-2",
        "documented-center",
        "north",
        "east",
        "west",
    }


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


@pytest.mark.asyncio
async def test_returns_cached_context_without_provider_call() -> None:
    """Проверяет, что cache hit исключает запрос к Geoapify."""

    provider = FakePlacesProvider()
    cached_context = build_context()
    cache = FakeTravelContextCache(
        cached_context=cached_context,
    )
    service = TripEnrichmentService(
        places_provider=provider,
        travel_context_cache=cache,
    )

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=3,
        )
    )

    assert context == cached_context
    assert cache.get_destination == "Стамбул"
    assert cache.get_categories == [
        "tourism.sights",
        "entertainment.museum",
    ]
    assert cache.saved_context is None
    assert provider.received_destination is None
    assert provider.received_categories is None


@pytest.mark.asyncio
async def test_saves_provider_result_after_cache_miss() -> None:
    """Проверяет сохранение результата Geoapify после cache miss."""

    provider = FakePlacesProvider(
        places=[build_place()],
    )
    cache = FakeTravelContextCache()
    service = TripEnrichmentService(
        places_provider=provider,
        travel_context_cache=cache,
    )

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=3,
        )
    )

    assert provider.received_destination == "Стамбул"
    assert cache.get_destination == "Стамбул"
    assert cache.set_destination == "Стамбул"
    assert cache.set_categories == [
        "tourism.sights",
        "entertainment.museum",
    ]
    assert cache.saved_context == context


@pytest.mark.asyncio
async def test_enriches_best_candidates_with_place_details() -> None:
    """Проверяет лимит и сохранение дополнительных сведений."""

    places = [
        build_place(
            name=f"Музей {index}",
            source_place_id=f"museum-{index}",
            categories=["entertainment.museum"],
            distance_meters=float(index * 100),
            available_details=[
                "details",
                "details.contact",
            ],
            wiki_reference_count=3,
        )
        for index in range(1, 7)
    ]

    details = {
        f"museum-{index}": PlaceDetails(
            source_place_id=f"museum-{index}",
            website=f"https://museum-{index}.example/",
            opening_hours="Mo-Su 09:00-18:00",
        )
        for index in range(1, 6)
    }

    provider = FakePlacesProvider(
        places=places,
        details=details,
    )
    service = TripEnrichmentService(provider)

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=2,
            interests="Музеи",
        )
    )

    assert provider.received_detail_place_ids == [
        "museum-1",
        "museum-2",
        "museum-3",
        "museum-4",
        "museum-5",
    ]

    places_by_id = {place.source_place_id: place for place in context.places}

    assert places_by_id["museum-1"].website == ("https://museum-1.example/")
    assert places_by_id["museum-1"].opening_hours == ("Mo-Su 09:00-18:00")
    assert places_by_id["museum-6"].website is None
    assert places_by_id["museum-6"].opening_hours is None


@pytest.mark.asyncio
async def test_place_details_error_does_not_cancel_enrichment() -> None:
    """Проверяет fail-open поведение Place Details."""

    place = build_place(
        available_details=[
            "details",
            "details.contact",
        ],
    )
    provider = FakePlacesProvider(
        places=[place],
        details_error=GeoapifyServiceError("Place Details недоступен."),
    )
    service = TripEnrichmentService(provider)

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=1,
        )
    )

    assert provider.received_detail_place_ids == [
        "hagia-sophia-id",
    ]
    assert context.places[0].website is None
    assert context.places[0].opening_hours is None


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        (["entertainment.museum"], True),
        (["building.tourism"], True),
        (["tourism.sights.castle"], True),
        (["tourism.sights.memorial.necropolis"], True),
        (["fee", "tourism.sights.archaeological_site"], True),
        (["tourism.sights.square"], False),
        (["tourism.sights.memorial.monument"], False),
    ],
)
def test_selects_only_schedule_sensitive_fallback_categories(
    categories: list[str],
    expected: bool,
) -> None:
    """Проверяет фильтр, полученный из аудита пяти направлений."""

    place = build_place(
        categories=categories,
        available_details=["details"],
    )

    assert requires_opening_hours_fallback(place) is expected


def test_does_not_request_fallback_for_existing_hours() -> None:
    """Geoapify всегда имеет приоритет перед Google."""

    place = build_place(
        categories=["entertainment.museum"],
        available_details=["details"],
    ).model_copy(update={"opening_hours": "Mo-Fr 09:00-17:00"})

    assert requires_opening_hours_fallback(place) is False


@pytest.mark.asyncio
async def test_enriches_cached_context_with_google_hours_without_saving_them() -> None:
    """Добавляет fallback после чтения кеша и не записывает Google-данные."""

    museum = build_place(
        name="Военный музей",
        source_place_id="museum-id",
        categories=["entertainment.museum"],
        available_details=["details", "details.contact"],
    )
    square = build_place(
        name="Историческая площадь",
        source_place_id="square-id",
        categories=["tourism.sights.square"],
        available_details=["details"],
    )
    cached_context = build_context().model_copy(update={"places": [museum, square]})
    cache = FakeTravelContextCache(cached_context=cached_context)
    fallback_provider = FakeOpeningHoursFallbackProvider(
        hours={"museum-id": "We-Su 09:00-17:00"}
    )
    budget = FakeOpeningHoursBudget()
    service = TripEnrichmentService(
        places_provider=FakePlacesProvider(),
        travel_context_cache=cache,
        opening_hours_fallback_provider=fallback_provider,
        opening_hours_budget=budget,
    )

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=2,
        )
    )

    places_by_id = {place.source_place_id: place for place in context.places}

    assert fallback_provider.received_place_ids == ["museum-id"]
    assert budget.calls == 1
    assert places_by_id["museum-id"].opening_hours == "We-Su 09:00-17:00"
    assert places_by_id["museum-id"].opening_hours_source == "google"
    assert places_by_id["square-id"].opening_hours is None
    assert cache.saved_context is None
    assert cached_context.places[0].opening_hours is None


@pytest.mark.asyncio
async def test_applies_relocation_only_to_current_context() -> None:
    """Переносит место после кеша и не меняет кешированные данные."""

    museum = build_place(
        name="Полицейский музей",
        source_place_id="museum-id",
        categories=["entertainment.museum"],
        available_details=["details", "details.contact"],
    ).model_copy(
        update={
            "website": "https://museum.example/current",
        }
    )
    cached_context = build_context().model_copy(update={"places": [museum]})
    cache = FakeTravelContextCache(cached_context=cached_context)
    fallback_provider = FakeOpeningHoursFallbackProvider(
        updates={
            "museum-id": {
                "formatted_address": ("Новый адрес музея, Стамбул"),
                "latitude": 41.0200,
                "longitude": 28.9900,
                "distance_meters": None,
                "location_source": "google",
                "opening_hours": "Tu-Su 09:30-16:00",
                "opening_hours_source": "google",
            }
        }
    )
    service = TripEnrichmentService(
        places_provider=FakePlacesProvider(),
        travel_context_cache=cache,
        opening_hours_fallback_provider=fallback_provider,
        opening_hours_budget=FakeOpeningHoursBudget(),
    )

    context = await service.enrich(
        TripPreferences(
            destination="Стамбул",
            duration_days=1,
        )
    )

    relocated_place = context.places[0]

    assert relocated_place.formatted_address == ("Новый адрес музея, Стамбул")
    assert relocated_place.location_source == "google"
    assert relocated_place.opening_hours_source == "google"
    assert relocated_place.distance_meters is not None
    assert relocated_place.distance_meters > 0
    assert cached_context.places[0].formatted_address == ("Султанахмет, Стамбул")
    assert cached_context.places[0].location_source == "geoapify"
    assert cache.saved_context is None


@pytest.mark.asyncio
async def test_budget_denial_skips_google_fallback() -> None:
    """Не вызывает Google после исчерпания месячного лимита."""

    museum = build_place(
        categories=["entertainment.museum"],
        available_details=["details"],
    )
    cache = FakeTravelContextCache(
        cached_context=build_context().model_copy(update={"places": [museum]})
    )
    fallback_provider = FakeOpeningHoursFallbackProvider()
    service = TripEnrichmentService(
        places_provider=FakePlacesProvider(),
        travel_context_cache=cache,
        opening_hours_fallback_provider=fallback_provider,
        opening_hours_budget=FakeOpeningHoursBudget(allowed=False),
    )

    context = await service.enrich(
        TripPreferences(destination="Стамбул", duration_days=1)
    )

    assert fallback_provider.received_place_ids == []
    assert context.places[0].opening_hours is None


@pytest.mark.asyncio
async def test_google_error_does_not_cancel_trip_enrichment() -> None:
    """Ошибка необязательного fallback не отменяет маршрут."""

    museum = build_place(
        categories=["entertainment.museum"],
        available_details=["details"],
    )
    cache = FakeTravelContextCache(
        cached_context=build_context().model_copy(update={"places": [museum]})
    )
    service = TripEnrichmentService(
        places_provider=FakePlacesProvider(),
        travel_context_cache=cache,
        opening_hours_fallback_provider=FakeOpeningHoursFallbackProvider(
            error=GooglePlacesServiceError("unavailable")
        ),
        opening_hours_budget=FakeOpeningHoursBudget(),
    )

    context = await service.enrich(
        TripPreferences(destination="Стамбул", duration_days=1)
    )

    assert context.places[0].opening_hours is None
