"""Unit-тесты ограниченного Google Places fallback."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.schemas.geoapify import DestinationLocation, PlaceCandidate
from app.schemas.google_places import GoogleOpeningHours
from app.services.google_places import (
    GOOGLE_REQUIRED_SEARCH_RADIUS_METERS,
    GOOGLE_TEXT_SEARCH_FIELD_MASK,
    GooglePlacesBudgetUnavailableError,
    GooglePlacesClient,
    GooglePlacesMonthlyBudget,
    GooglePlacesServiceError,
    format_google_opening_hours,
)


def build_place() -> PlaceCandidate:
    """Создаёт исходное место Geoapify."""

    return PlaceCandidate(
        name="Askerî Müze",
        formatted_address="Harbiye, Istanbul, Türkiye",
        latitude=41.0475,
        longitude=28.9887,
        categories=["entertainment.museum"],
        available_details=["details", "details.contact"],
        source_place_id="geoapify-museum-id",
    )


def build_location() -> DestinationLocation:
    """Создаёт центр обязательного поиска."""

    return DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )


def build_client(
    handler: Any,
) -> tuple[httpx.AsyncClient, GooglePlacesClient]:
    """Создаёт клиент с управляемым HTTP transport."""

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    )
    google_client = GooglePlacesClient(
        client=http_client,
        api_key=SecretStr("test-google-key"),
        base_url="https://places.googleapis.test",
        timeout_seconds=5.0,
    )

    return http_client, google_client


@pytest.mark.asyncio
async def test_enrich_place_matches_and_formats_schedule() -> None:
    """Проверяет запрос, сопоставление и расписание."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == ("https://places.googleapis.test/v1/places:searchText")
        assert request.headers["X-Goog-Api-Key"] == ("test-google-key")
        assert request.headers["X-Goog-FieldMask"] == (GOOGLE_TEXT_SEARCH_FIELD_MASK)

        request_data = json.loads(request.content)

        assert request_data["maxResultCount"] == 3
        assert request_data["locationBias"]["circle"]["radius"] == 1000.0

        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "google-museum-id",
                        "displayName": {"text": "Askeri Muze"},
                        "location": {
                            "latitude": 41.0476,
                            "longitude": 28.9888,
                        },
                        "regularOpeningHours": {
                            "periods": [
                                {
                                    "open": {
                                        "day": day,
                                        "hour": 9,
                                    },
                                    "close": {
                                        "day": day,
                                        "hour": 17,
                                    },
                                }
                                for day in range(1, 6)
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        enriched_place = await client.enrich_place(build_place())

    assert enriched_place.opening_hours == (
        "Mo 09:00-17:00; Tu 09:00-17:00; We 09:00-17:00; Th 09:00-17:00; Fr 09:00-17:00"
    )
    assert enriched_place.opening_hours_source == "google"
    assert enriched_place.location_source == "geoapify"


@pytest.mark.asyncio
async def test_search_required_place_builds_google_candidate() -> None:
    """Преобразует точный Google-результат во внутреннее место HankeGo."""

    def handler(request: httpx.Request) -> httpx.Response:
        request_data = json.loads(request.content)

        assert request_data["textQuery"] == ("Цистерна Базилика, Стамбул, Турция")
        assert request_data["maxResultCount"] == 5
        assert request_data["locationBias"]["circle"]["radius"] == (
            GOOGLE_REQUIRED_SEARCH_RADIUS_METERS
        )

        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "basilica-cistern-id",
                        "displayName": {"text": "Цистерна Базилика"},
                        "formattedAddress": "Alemdar, Istanbul, Türkiye",
                        "types": ["tourist_attraction"],
                        "websiteUri": "https://yerebatan.com/",
                        "location": {
                            "latitude": 41.0084,
                            "longitude": 28.9779,
                        },
                        "regularOpeningHours": {
                            "periods": [
                                {
                                    "open": {"day": 1, "hour": 9},
                                    "close": {"day": 1, "hour": 18},
                                }
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        place = await client.search_required_place(
            required_name="Цистерна Базилика",
            location=build_location(),
        )

    assert place is not None
    assert place.name == "Цистерна Базилика"
    assert place.source_place_id == "google:basilica-cistern-id"
    assert place.source == "google"
    assert place.location_source == "google"
    assert place.opening_hours_source == "google"
    assert place.opening_hours == "Mo 09:00-18:00"
    assert place.website == "https://yerebatan.com/"
    assert place.distance_meters is not None


@pytest.mark.asyncio
async def test_search_required_place_rejects_unrelated_result() -> None:
    """Не подменяет обязательное место похожим объектом Google."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "unrelated-id",
                        "displayName": {"text": "Случайное кафе"},
                        "formattedAddress": "Стамбул, Турция",
                        "location": {
                            "latitude": 41.0084,
                            "longitude": 28.9779,
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        place = await client.search_required_place(
            required_name="Цистерна Базилика",
            location=build_location(),
        )

    assert place is None


@pytest.mark.asyncio
async def test_search_required_place_accepts_unique_translated_result() -> None:
    """Принимает единственный результат с названием в другой письменности."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "meguro-parasitological-museum",
                        "displayName": {
                            "text": "Meguro Parasitological Museum",
                        },
                        "formattedAddress": ("4-chome-1-1 Shimomeguro, Tokyo, Japan"),
                        "types": [
                            "tourist_attraction",
                            "museum",
                        ],
                        "businessStatus": "OPERATIONAL",
                        "location": {
                            "latitude": 35.6336,
                            "longitude": 139.7088,
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        place = await client.search_required_place(
            required_name="目黒寄生虫館",
            location=DestinationLocation(
                formatted_name="Токио, Япония",
                latitude=35.6764,
                longitude=139.6500,
                source_place_id="tokyo-place-id",
            ),
        )

    assert place is not None
    assert place.name == "Meguro Parasitological Museum"
    assert place.source_place_id == ("google:meguro-parasitological-museum")


@pytest.mark.asyncio
async def test_search_required_place_rejects_multiple_translated_results() -> None:
    """Не угадывает между несколькими названиями в другой письменности."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": place_id,
                        "displayName": {"text": name},
                        "formattedAddress": "Tokyo, Japan",
                        "location": {
                            "latitude": latitude,
                            "longitude": 139.7088,
                        },
                    }
                    for place_id, name, latitude in (
                        (
                            "first-museum",
                            "Meguro Parasitological Museum",
                            35.6336,
                        ),
                        (
                            "second-museum",
                            "Tokyo Museum",
                            35.6400,
                        ),
                    )
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        place = await client.search_required_place(
            required_name="目黒寄生虫館",
            location=DestinationLocation(
                formatted_name="Токио, Япония",
                latitude=35.6764,
                longitude=139.6500,
                source_place_id="tokyo-place-id",
            ),
        )

    assert place is None


@pytest.mark.asyncio
async def test_enrich_place_rejects_unrelated_place() -> None:
    """Не принимает далёкий объект с другим названием."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "wrong-id",
                        "displayName": {"text": "Unrelated cafe"},
                        "location": {
                            "latitude": 41.5,
                            "longitude": 29.5,
                        },
                        "regularOpeningHours": {
                            "periods": [
                                {
                                    "open": {
                                        "day": 1,
                                        "hour": 9,
                                    },
                                    "close": {
                                        "day": 1,
                                        "hour": 17,
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        )

    source_place = build_place()
    http_client, client = build_client(handler)

    async with http_client:
        enriched_place = await client.enrich_place(source_place)

    assert enriched_place == source_place


@pytest.mark.asyncio
async def test_enrich_place_accepts_nearby_translated_result() -> None:
    """Принимает перевод названия у ближайшего результата."""

    source_place = build_place().model_copy(
        update={
            "name": "Большой Кремлёвский дворец",
            "formatted_address": "Москва, Россия",
            "latitude": 55.7501,
            "longitude": 37.6156,
        }
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "grand-kremlin-palace",
                        "displayName": {"text": "Grand Kremlin Palace"},
                        "location": {
                            "latitude": 55.7502,
                            "longitude": 37.6157,
                        },
                        "regularOpeningHours": {
                            "periods": [
                                {
                                    "open": {
                                        "day": 3,
                                        "hour": 10,
                                    },
                                    "close": {
                                        "day": 3,
                                        "hour": 17,
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        enriched_place = await client.enrich_place(source_place)

    assert enriched_place.opening_hours == "We 10:00-17:00"
    assert enriched_place.location_source == "geoapify"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "business_status",
    [
        "CLOSED_TEMPORARILY",
        "CLOSED_PERMANENTLY",
    ],
)
async def test_enrich_place_marks_closed_place_as_off(
    business_status: str,
) -> None:
    """Передаёт закрытый статус как отсутствие периодов."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "google-museum-id",
                        "displayName": {"text": "Askeri Muze"},
                        "location": {
                            "latitude": 41.0476,
                            "longitude": 28.9888,
                        },
                        "businessStatus": business_status,
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        enriched_place = await client.enrich_place(build_place())

    assert enriched_place.opening_hours == "off"
    assert enriched_place.opening_hours_source == "google"


@pytest.mark.asyncio
async def test_enrich_place_accepts_verified_relocation() -> None:
    """Принимает перенос по совпавшей странице объекта."""

    website = (
        "https://www.keishicho.metro.tokyo.lg.jp/"
        "about_mpd/welcome/welcome/museum_tour.html"
    )
    source_place = PlaceCandidate(
        name="警察博物館",
        formatted_address="京橋三丁目, Токио, Япония",
        latitude=35.6751234,
        longitude=139.769582,
        categories=["entertainment.museum"],
        available_details=["details", "details.contact"],
        website=website,
        source_place_id="geoapify-police-museum",
    )
    current_address = (
        "Japan, Tokyo, Shinagawa City, Nishigotanda, 7-chōme-22-17 Toc Building, 3F"
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "google-police-museum",
                        "displayName": {"text": "Police Museum"},
                        "formattedAddress": current_address,
                        "types": [
                            "museum",
                            "history_museum",
                        ],
                        "websiteUri": website,
                        "location": {
                            "latitude": 35.6218791,
                            "longitude": 139.7190516,
                        },
                        "businessStatus": "OPERATIONAL",
                        "regularOpeningHours": {
                            "periods": [
                                {
                                    "open": {
                                        "day": 2,
                                        "hour": 9,
                                        "minute": 30,
                                    },
                                    "close": {
                                        "day": 2,
                                        "hour": 16,
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        enriched_place = await client.enrich_place(source_place)

    assert enriched_place.formatted_address == current_address
    assert enriched_place.latitude == 35.6218791
    assert enriched_place.longitude == 139.7190516
    assert enriched_place.location_source == "google"
    assert enriched_place.opening_hours == "Tu 09:30-16:00"
    assert enriched_place.opening_hours_source == "google"
    assert enriched_place.source_place_id == ("geoapify-police-museum")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "google_website",
        "google_types",
        "latitude",
        "longitude",
    ),
    [
        (
            "https://example.com/different-place",
            ["museum"],
            35.6218791,
            139.7190516,
        ),
        (
            (
                "https://www.keishicho.metro.tokyo.lg.jp/"
                "about_mpd/welcome/welcome/museum_tour.html"
            ),
            ["cafe"],
            35.6218791,
            139.7190516,
        ),
        (
            (
                "https://www.keishicho.metro.tokyo.lg.jp/"
                "about_mpd/welcome/welcome/museum_tour.html"
            ),
            ["museum"],
            34.6937,
            135.5023,
        ),
    ],
)
async def test_enrich_place_rejects_unverified_relocation(
    google_website: str,
    google_types: list[str],
    latitude: float,
    longitude: float,
) -> None:
    """Не переносит место при недостаточных доказательствах."""

    source_website = (
        "https://www.keishicho.metro.tokyo.lg.jp/"
        "about_mpd/welcome/welcome/museum_tour.html"
    )
    source_place = PlaceCandidate(
        name="警察博物館",
        formatted_address="京橋三丁目, Токио, Япония",
        latitude=35.6751234,
        longitude=139.769582,
        categories=["entertainment.museum"],
        available_details=["details", "details.contact"],
        website=source_website,
        source_place_id="geoapify-police-museum",
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "places": [
                    {
                        "id": "google-police-museum",
                        "displayName": {"text": "Police Museum"},
                        "formattedAddress": ("Current Google address"),
                        "types": google_types,
                        "websiteUri": google_website,
                        "location": {
                            "latitude": latitude,
                            "longitude": longitude,
                        },
                        "regularOpeningHours": {
                            "periods": [
                                {
                                    "open": {
                                        "day": 2,
                                        "hour": 9,
                                    },
                                    "close": {
                                        "day": 2,
                                        "hour": 16,
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        enriched_place = await client.enrich_place(source_place)

    assert enriched_place == source_place


@pytest.mark.asyncio
async def test_enrich_place_converts_provider_error() -> None:
    """Не раскрывает тело ошибочного ответа Google."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            json={"error": {"message": "secret provider details"}},
        )

    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(
            GooglePlacesServiceError,
            match="вернул ошибку",
        ):
            await client.enrich_place(build_place())


def test_formats_overnight_opening_period() -> None:
    """Разбивает ночной интервал между двумя днями."""

    opening_hours = GoogleOpeningHours.model_validate(
        {
            "periods": [
                {
                    "open": {"day": 5, "hour": 22},
                    "close": {"day": 6, "hour": 2},
                }
            ]
        }
    )

    assert format_google_opening_hours(opening_hours) == (
        "Fr 22:00-24:00; Sa 00:00-02:00"
    )


def test_formats_always_open_place() -> None:
    """Распознаёт документированный Google-формат 24/7."""

    opening_hours = GoogleOpeningHours.model_validate(
        {"periods": [{"open": {"day": 0, "hour": 0}}]}
    )

    assert format_google_opening_hours(opening_hours) == "24/7"


class FakeRedisBudgetClient:
    """Управляемый Redis для проверки месячного бюджета."""

    def __init__(
        self,
        *,
        result: Any = 1,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.received_args: tuple[Any, ...] | None = None

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> Any:
        """Возвращает заданное значение счётчика."""

        self.received_args = (script, numkeys, *keys_and_args)

        if self.error is not None:
            raise self.error

        return self.result


@pytest.mark.asyncio
async def test_monthly_budget_allows_request_within_limit() -> None:
    """Разрешает запрос внутри месячного лимита."""

    redis_client = FakeRedisBudgetClient(result=900)
    budget = GooglePlacesMonthlyBudget(
        redis_client=redis_client,
        monthly_limit=900,
    )

    assert await budget.try_acquire() is True
    assert redis_client.received_args is not None
    assert redis_client.received_args[1] == 1
    assert str(redis_client.received_args[2]).startswith("budget:google-places:")
    assert int(redis_client.received_args[3]) > 0


@pytest.mark.asyncio
async def test_monthly_budget_rejects_request_above_limit() -> None:
    """Запрещает запрос после исчерпания лимита."""

    budget = GooglePlacesMonthlyBudget(
        redis_client=FakeRedisBudgetClient(result=901),
        monthly_limit=900,
    )

    assert await budget.try_acquire() is False


@pytest.mark.asyncio
async def test_monthly_budget_fails_closed_without_redis() -> None:
    """Не разрешает платный запрос, если счётчик недоступен."""

    budget = GooglePlacesMonthlyBudget(
        redis_client=FakeRedisBudgetClient(error=RedisError("unavailable")),
        monthly_limit=900,
    )

    with pytest.raises(GooglePlacesBudgetUnavailableError):
        await budget.try_acquire()
