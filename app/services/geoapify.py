"""
Асинхронный клиент Geoapify.

Модуль отвечает только за HTTP-интеграцию с провайдером:
- формирует запросы;
- добавляет API key;
- проверяет HTTP-ответы;
- валидирует JSON через Pydantic;
- преобразует внешний формат во внутренние модели HankeGo.
"""

import json
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from app.schemas.geoapify import (
    DestinationLocation,
    GeoapifyGeocodingResponse,
    GeoapifyPlacesResponse,
    PlaceCandidate,
)


class GeoapifyServiceError(Exception):
    """Безопасная ошибка интеграции с Geoapify."""


class GeoapifyRateLimitError(GeoapifyServiceError):
    """Дневной или кратковременный лимит Geoapify исчерпан."""


class DestinationNotFoundError(GeoapifyServiceError):
    """Geoapify не смог определить указанное направление."""


class GeoapifyClient:
    """
    Выполняет запросы к Geoapify через переданный AsyncClient.

    HTTP-клиент передаётся снаружи, чтобы соединения можно было
    переиспользовать, а в тестах подставлять MockTransport.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        base_url: str,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def _get_json(
        self,
        *,
        path: str,
        params: dict[str, str | int],
    ) -> dict[str, Any]:
        """Выполняет GET-запрос и возвращает JSON-объект."""

        request_params = {
            **params,
            "apiKey": self._api_key.get_secret_value(),
        }

        try:
            response = await self._client.get(
                url=f"{self._base_url}{path}",
                params=request_params,
            )

        except httpx.TimeoutException as error:
            raise GeoapifyServiceError(
                "Сервис туристических данных временно не отвечает."
            ) from error

        except httpx.RequestError as error:
            raise GeoapifyServiceError(
                "Не удалось подключиться к сервису туристических данных."
            ) from error

        if response.status_code == 429:
            raise GeoapifyRateLimitError(
                "Лимит сервиса туристических данных временно исчерпан."
            )

        if response.status_code in {401, 403}:
            raise GeoapifyServiceError(
                "Сервис туристических данных неправильно настроен."
            )

        try:
            response.raise_for_status()

        except httpx.HTTPStatusError as error:
            raise GeoapifyServiceError(
                "Сервис туристических данных временно недоступен."
            ) from error

        try:
            response_data = response.json()

        except json.JSONDecodeError as error:
            raise GeoapifyServiceError(
                "Сервис туристических данных вернул некорректный ответ."
            ) from error

        if not isinstance(response_data, dict):
            raise GeoapifyServiceError(
                "Сервис туристических данных вернул некорректный ответ."
            )

        return response_data

    async def geocode_destination(
        self,
        destination: str,
    ) -> DestinationLocation:
        """Определяет координаты и place_id направления."""

        response_data = await self._get_json(
            path="/v1/geocode/search",
            params={
                "text": destination,
                "format": "geojson",
                "lang": "ru",
                "type": "city",
                "limit": 1,
            },
        )

        try:
            response = GeoapifyGeocodingResponse.model_validate(response_data)

        except ValidationError as error:
            raise GeoapifyServiceError(
                "Сервис туристических данных вернул некорректный ответ."
            ) from error

        if not response.features:
            raise DestinationNotFoundError(
                "Не удалось определить указанное направление."
            )

        properties = response.features[0].properties

        return DestinationLocation(
            formatted_name=properties.formatted,
            latitude=properties.lat,
            longitude=properties.lon,
            source_place_id=properties.place_id,
        )

    async def search_places(
        self,
        *,
        location: DestinationLocation,
        categories: list[str],
        limit: int = 20,
        radius_meters: int = 15_000,
    ) -> list[PlaceCandidate]:
        """Ищет именованные места рядом с центром направления."""

        if not categories:
            raise ValueError("Для поиска нужна хотя бы одна категория.")

        if not 1 <= limit <= 60:
            raise ValueError("Количество мест должно быть от 1 до 60.")

        if not 1_000 <= radius_meters <= 50_000:
            raise ValueError("Радиус поиска должен быть от 1000 до 50000 метров.")

        spatial_filter = (
            f"circle:{location.longitude},{location.latitude},{radius_meters}"
        )

        response_data = await self._get_json(
            path="/v2/places",
            params={
                "categories": ",".join(categories),
                "filter": spatial_filter,
                "bias": (f"proximity:{location.longitude},{location.latitude}"),
                "lang": "ru",
                "limit": limit,
            },
        )

        try:
            response = GeoapifyPlacesResponse.model_validate(response_data)

        except ValidationError as error:
            raise GeoapifyServiceError(
                "Сервис туристических данных вернул некорректный ответ."
            ) from error

        places: list[PlaceCandidate] = []

        for feature in response.features:
            properties = feature.properties

            if properties.name is None:
                continue

            wiki_and_media = properties.wiki_and_media

            if wiki_and_media is None:
                wiki_reference_count = 0
            else:
                wiki_reference_count = sum(
                    1
                    for value in (
                        wiki_and_media.wikidata,
                        wiki_and_media.wikipedia,
                        wiki_and_media.wikimedia_commons,
                        wiki_and_media.image,
                    )
                    if value is not None and value.strip()
                )

            places.append(
                PlaceCandidate(
                    name=properties.name,
                    formatted_address=properties.formatted,
                    latitude=properties.lat,
                    longitude=properties.lon,
                    categories=properties.categories,
                    distance_meters=properties.distance,
                    available_details=properties.details,
                    wiki_reference_count=(wiki_reference_count),
                    source_place_id=properties.place_id,
                )
            )

        return places
