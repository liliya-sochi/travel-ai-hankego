"""Unit-тесты ограниченного Google Places fallback."""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.schemas.geoapify import PlaceCandidate
from app.schemas.google_places import GoogleOpeningHours
from app.services.google_places import (
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
async def test_get_opening_hours_matches_place_and_formats_schedule() -> None:
    """Проверяет запрос, сопоставление места и недельное расписание."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://places.googleapis.test/v1/places:searchText"
        assert request.headers["X-Goog-Api-Key"] == "test-google-key"
        assert request.headers["X-Goog-FieldMask"] == GOOGLE_TEXT_SEARCH_FIELD_MASK

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
                                    "open": {"day": day, "hour": 9},
                                    "close": {"day": day, "hour": 17},
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
        opening_hours = await client.get_opening_hours(build_place())

    assert opening_hours == (
        "Mo 09:00-17:00; Tu 09:00-17:00; We 09:00-17:00; Th 09:00-17:00; Fr 09:00-17:00"
    )


@pytest.mark.asyncio
async def test_get_opening_hours_rejects_unrelated_place() -> None:
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
                                    "open": {"day": 1, "hour": 9},
                                    "close": {"day": 1, "hour": 17},
                                }
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        opening_hours = await client.get_opening_hours(build_place())

    assert opening_hours is None


@pytest.mark.asyncio
async def test_get_opening_hours_accepts_nearby_translated_first_result() -> None:
    """Принимает перевод названия только у ближайшего первого результата."""

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
                                    "open": {"day": 3, "hour": 10},
                                    "close": {"day": 3, "hour": 17},
                                }
                            ]
                        },
                    }
                ]
            },
        )

    http_client, client = build_client(handler)

    async with http_client:
        opening_hours = await client.get_opening_hours(source_place)

    assert opening_hours == "We 10:00-17:00"


@pytest.mark.asyncio
async def test_get_opening_hours_converts_provider_error() -> None:
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
            await client.get_opening_hours(build_place())


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
