"""Unit-тесты асинхронного Geoapify client."""

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app.schemas.geoapify import DestinationLocation
from app.services.geoapify import (
    DestinationNotFoundError,
    GeoapifyClient,
    GeoapifyRateLimitError,
    GeoapifyServiceError,
)

TEST_API_KEY = "test-geoapify-api-key"
TEST_BASE_URL = "https://geoapify.example"


def build_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.AsyncClient, GeoapifyClient]:
    """Создаёт HTTP-клиент с управляемым тестовым transport."""

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    )
    geoapify_client = GeoapifyClient(
        client=http_client,
        api_key=SecretStr(TEST_API_KEY),
        base_url=TEST_BASE_URL,
    )
    return http_client, geoapify_client


@pytest.mark.asyncio
async def test_geocode_destination() -> None:
    """Проверяет геокодирование направления."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/geocode/search"
        assert request.url.params["text"] == "Стамбул"
        assert request.url.params["format"] == "geojson"
        assert request.url.params["type"] == "city"
        assert request.url.params["limit"] == "1"
        assert request.url.params["apiKey"] == TEST_API_KEY

        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "formatted": "Стамбул, Турция",
                            "lat": 41.0082,
                            "lon": 28.9784,
                            "place_id": "istanbul-place-id",
                            "unexpected_field": "ignored",
                        },
                    }
                ],
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        location = await client.geocode_destination("Стамбул")

    assert location == DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )


@pytest.mark.asyncio
async def test_geocode_rejects_empty_result() -> None:
    """Проверяет понятную ошибку неизвестного направления."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [],
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(DestinationNotFoundError):
            await client.geocode_destination("Несуществующее место")


@pytest.mark.asyncio
async def test_search_places_skips_unnamed_objects() -> None:
    """Проверяет поиск мест и новые ranking-метаданные."""

    requested_biases: list[str] = []
    requested_limits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/places"
        assert request.url.params["categories"] == (
            "tourism.sights,entertainment.museum"
        )
        assert request.url.params["filter"] == ("circle:28.9784,41.0082,15000")
        requested_biases.append(request.url.params["bias"])
        requested_limits.append(int(request.url.params["limit"]))

        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "name": "Айя-София",
                            "formatted": "Султанахмет, Стамбул",
                            "lat": 41.0086,
                            "lon": 28.9802,
                            "categories": [
                                "tourism.sights",
                                "religion.place_of_worship",
                            ],
                            "distance": 43,
                            "details": [
                                "details",
                                "details.contact",
                                "details.wiki_and_media",
                            ],
                            "wiki_and_media": {
                                "wikidata": "Q12506",
                                "wikipedia": "tr:Ayasofya",
                                "wikimedia_commons": "Category:Hagia Sophia",
                            },
                            "place_id": "hagia-sophia-id",
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "formatted": "Стамбул, Турция",
                            "lat": 41.01,
                            "lon": 28.98,
                            "categories": ["tourism.sights"],
                            "distance": 100,
                            "place_id": "unnamed-place-id",
                        },
                    },
                ],
            },
        )

    location = DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )
    http_client, client = build_client(handler)

    async with http_client:
        places = await client.search_places(
            location=location,
            categories=[
                "tourism.sights",
                "entertainment.museum",
            ],
            limit=120,
        )

    assert len(requested_biases) == 5
    assert len(set(requested_biases)) == 5
    assert sorted(requested_limits) == [15, 15, 15, 15, 60]
    assert len(places) == 1

    place = places[0]

    assert place.name == "Айя-София"
    assert place.source_place_id == "hagia-sophia-id"
    assert place.distance_meters == pytest.approx(
        157.0,
        abs=2.0,
    )
    assert place.available_details == [
        "details",
        "details.contact",
        "details.wiki_and_media",
    ]
    assert place.wiki_reference_count == 3
    assert place.source == "geoapify"


@pytest.mark.asyncio
async def test_search_places_uses_successful_anchors_after_partial_error() -> None:
    """Проверяет fail-open при ошибке одной точки поиска."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["bias"] == "proximity:28.9784,41.0082":
            return httpx.Response(
                status_code=503,
                json={"message": "temporarily unavailable"},
            )

        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "name": "Удалённый музей",
                            "formatted": "Стамбул, Турция",
                            "lat": 41.04,
                            "lon": 29.01,
                            "categories": ["entertainment.museum"],
                            "place_id": "remote-museum-id",
                        },
                    }
                ],
            },
        )

    location = DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )
    http_client, client = build_client(handler)

    async with http_client:
        places = await client.search_places(
            location=location,
            categories=["entertainment.museum"],
            limit=20,
        )

    assert [place.source_place_id for place in places] == [
        "remote-museum-id",
    ]


@pytest.mark.asyncio
async def test_search_places_raises_if_all_anchors_fail() -> None:
    """Проверяет ошибку, если не сработала ни одна точка поиска."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=503,
            json={"message": "temporarily unavailable"},
        )

    location = DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )
    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(GeoapifyServiceError):
            await client.search_places(
                location=location,
                categories=["entertainment.museum"],
                limit=20,
            )


@pytest.mark.asyncio
async def test_get_place_details() -> None:
    """Проверяет получение сайта и часов работы места."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/place-details"
        assert request.url.params["id"] == "museum-place-id"
        assert request.url.params["features"] == "details"
        assert request.url.params["lang"] == "ru"
        assert request.url.params["apiKey"] == TEST_API_KEY

        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "feature_type": "details",
                            "name": "Тестовый музей",
                            "formatted": "Стамбул, Турция",
                            "lat": 41.0082,
                            "lon": 28.9784,
                            "place_id": ("details-feature-place-id"),
                            "website": " https://museum.example/ ",
                            "opening_hours": " Mo-Su 09:00-18:30 ",
                        },
                    }
                ],
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        details = await client.get_place_details("museum-place-id")

    """Проверяет детали и сохранение ID исходного кандидата."""
    assert details.source_place_id == "museum-place-id"
    assert details.website == "https://museum.example/"
    assert details.opening_hours == "Mo-Su 09:00-18:30"
    assert details.source == "geoapify"


@pytest.mark.asyncio
async def test_get_place_details_rejects_missing_details() -> None:
    """Проверяет ответ без объекта feature_type=details."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [],
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(GeoapifyServiceError):
            await client.get_place_details("museum-place-id")


@pytest.mark.asyncio
async def test_rejects_invalid_places_limit() -> None:
    """Проверяет защиту от лишнего расходования credits."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP-запрос не должен выполняться.")

    location = DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )
    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(ValueError):
            await client.search_places(
                location=location,
                categories=["tourism.sights"],
                limit=121,
            )


@pytest.mark.asyncio
async def test_rejects_invalid_search_radius() -> None:
    """Проверяет ограничение территории поиска."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP-запрос не должен выполняться.")

    location = DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )
    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(ValueError):
            await client.search_places(
                location=location,
                categories=["tourism.sights"],
                radius_meters=100_000,
            )


@pytest.mark.asyncio
async def test_handles_rate_limit() -> None:
    """Проверяет отдельную ошибку HTTP 429."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            json={"message": "quota exceeded"},
        )

    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(GeoapifyRateLimitError):
            await client.geocode_destination("Стамбул")


@pytest.mark.asyncio
async def test_handles_invalid_json_structure() -> None:
    """Проверяет Pydantic-валидацию ответа провайдера."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "formatted": "Стамбул, Турция",
                            "lat": 500,
                            "lon": 28.9784,
                            "place_id": "istanbul-place-id",
                        },
                    }
                ],
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        with pytest.raises(GeoapifyServiceError):
            await client.geocode_destination("Стамбул")
