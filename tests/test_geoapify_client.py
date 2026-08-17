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
    """Проверяет поиск мест и фильтрацию объектов без имени."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/places"
        assert request.url.params["categories"] == (
            "tourism.sights,entertainment.museum"
        )
        assert request.url.params["filter"] == ("circle:28.9784,41.0082,15000")
        assert request.url.params["limit"] == "20"

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
        )

    assert len(places) == 1
    assert places[0].name == "Айя-София"
    assert places[0].source_place_id == "hagia-sophia-id"
    assert places[0].source == "geoapify"


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
                limit=21,
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
