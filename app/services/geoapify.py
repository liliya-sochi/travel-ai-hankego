"""
Асинхронный клиент Geoapify.

Модуль отвечает только за HTTP-интеграцию с провайдером:
- формирует запросы;
- добавляет API key;
- проверяет HTTP-ответы;
- валидирует JSON через Pydantic;
- преобразует внешний формат во внутренние модели HankeGo.
"""

import asyncio
import json
import logging
import math
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr, ValidationError

from app.schemas.geoapify import (
    DestinationLocation,
    GeoapifyGeocodingResponse,
    GeoapifyPlaceDetailsResponse,
    GeoapifyPlacesResponse,
    PlaceCandidate,
    PlaceDetails,
)

logger = logging.getLogger(__name__)

EARTH_RADIUS_METERS = 6_371_000
MAX_SEARCH_ANCHOR_DISTANCE_METERS = 5_000
SEARCH_ANCHOR_BEARINGS = (0.0, 90.0, 180.0, 270.0)


def _calculate_distance_meters(
    *,
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> float:
    """Вычисляет расстояние между двумя координатами."""

    start_latitude_radians = math.radians(start_latitude)
    end_latitude_radians = math.radians(end_latitude)
    latitude_difference = math.radians(end_latitude - start_latitude)
    longitude_difference = math.radians(end_longitude - start_longitude)

    haversine_value = (
        math.sin(latitude_difference / 2) ** 2
        + math.cos(start_latitude_radians)
        * math.cos(end_latitude_radians)
        * math.sin(longitude_difference / 2) ** 2
    )

    return (
        EARTH_RADIUS_METERS
        * 2
        * math.atan2(
            math.sqrt(haversine_value),
            math.sqrt(1 - haversine_value),
        )
    )


def _move_coordinate(
    *,
    latitude: float,
    longitude: float,
    distance_meters: float,
    bearing_degrees: float,
) -> tuple[float, float]:
    """Смещает координату на заданное расстояние и направление."""

    angular_distance = distance_meters / EARTH_RADIUS_METERS
    bearing = math.radians(bearing_degrees)
    latitude_radians = math.radians(latitude)
    longitude_radians = math.radians(longitude)

    moved_latitude = math.asin(
        math.sin(latitude_radians) * math.cos(angular_distance)
        + math.cos(latitude_radians) * math.sin(angular_distance) * math.cos(bearing)
    )
    moved_longitude = longitude_radians + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(latitude_radians),
        math.cos(angular_distance)
        - math.sin(latitude_radians) * math.sin(moved_latitude),
    )

    normalized_longitude = (math.degrees(moved_longitude) + 540) % 360 - 180

    return math.degrees(moved_latitude), normalized_longitude


def _build_search_anchors(
    *,
    location: DestinationLocation,
    radius_meters: int,
) -> list[tuple[float, float]]:
    """Создаёт центр и четыре точки для поиска в разных частях города."""

    anchor_distance = min(
        MAX_SEARCH_ANCHOR_DISTANCE_METERS,
        radius_meters / 3,
    )
    anchors = [
        (location.latitude, location.longitude),
    ]

    for bearing in SEARCH_ANCHOR_BEARINGS:
        anchors.append(
            _move_coordinate(
                latitude=location.latitude,
                longitude=location.longitude,
                distance_meters=anchor_distance,
                bearing_degrees=bearing,
            )
        )

    return anchors


def _normalize_optional_text(
    value: str | None,
) -> str | None:
    """Удаляет пробелы и преобразует пустую строку в None."""

    if value is None:
        return None

    normalized_value = value.strip()

    return normalized_value or None


def _normalize_website(
    value: str | None,
) -> str | None:
    """Оставляет только абсолютные HTTP- и HTTPS-ссылки."""

    normalized_value = _normalize_optional_text(value)

    if normalized_value is None:
        return None

    parsed_url = urlparse(normalized_value)

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return None

    return normalized_value


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
        """Ищет именованные места в нескольких частях направления."""

        if not categories:
            raise ValueError("Для поиска нужна хотя бы одна категория.")

        if not 1 <= limit <= 120:
            raise ValueError("Количество мест должно быть от 1 до 120.")

        if not 1_000 <= radius_meters <= 50_000:
            raise ValueError("Радиус поиска должен быть от 1000 до 50000 метров.")

        anchors = _build_search_anchors(
            location=location,
            radius_meters=radius_meters,
        )[: min(limit, 5)]
        if len(anchors) == 1:
            anchor_limits = [limit]
        else:
            satellite_count = len(anchors) - 1
            center_limit = min(
                max(1, int(limit * 0.5)),
                limit - satellite_count,
            )
            satellite_limit, remainder = divmod(
                limit - center_limit,
                satellite_count,
            )
            anchor_limits = [
                center_limit,
                *(
                    satellite_limit + (index < remainder)
                    for index in range(satellite_count)
                ),
            ]

        results = await asyncio.gather(
            *(
                self._search_places_near_anchor(
                    location=location,
                    categories=categories,
                    radius_meters=radius_meters,
                    anchor_latitude=anchor_latitude,
                    anchor_longitude=anchor_longitude,
                    limit=anchor_limit,
                )
                for (
                    anchor_latitude,
                    anchor_longitude,
                ), anchor_limit in zip(
                    anchors,
                    anchor_limits,
                    strict=True,
                )
            ),
            return_exceptions=True,
        )

        successful_results: list[list[PlaceCandidate]] = []
        provider_errors: list[GeoapifyServiceError] = []

        for result in results:
            if isinstance(result, GeoapifyServiceError):
                provider_errors.append(result)
                continue

            if isinstance(result, BaseException):
                raise result

            successful_results.append(result)

        if not successful_results:
            raise provider_errors[0]

        if provider_errors:
            logger.warning(
                "Some Geoapify search anchors failed",
                extra={
                    "failed_anchor_count": len(provider_errors),
                    "total_anchor_count": len(anchors),
                },
            )

        unique_places: dict[str, PlaceCandidate] = {}

        for places in successful_results:
            for place in places:
                unique_places.setdefault(
                    place.source_place_id,
                    place,
                )

        return list(unique_places.values())

    async def _search_places_near_anchor(
        self,
        *,
        location: DestinationLocation,
        categories: list[str],
        radius_meters: int,
        anchor_latitude: float,
        anchor_longitude: float,
        limit: int,
    ) -> list[PlaceCandidate]:
        """Загружает и нормализует места около одной точки поиска."""

        response_data = await self._get_json(
            path="/v2/places",
            params={
                "categories": ",".join(categories),
                "filter": (
                    f"circle:{location.longitude},{location.latitude},{radius_meters}"
                ),
                "bias": f"proximity:{anchor_longitude},{anchor_latitude}",
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
                    distance_meters=_calculate_distance_meters(
                        start_latitude=location.latitude,
                        start_longitude=location.longitude,
                        end_latitude=properties.lat,
                        end_longitude=properties.lon,
                    ),
                    available_details=properties.details,
                    wiki_reference_count=wiki_reference_count,
                    source_place_id=properties.place_id,
                )
            )

        return places

    async def get_place_details(
        self,
        source_place_id: str,
    ) -> PlaceDetails:
        """Получает дополнительные сведения об одном месте."""

        normalized_place_id = source_place_id.strip()

        if not normalized_place_id:
            raise ValueError("Для получения деталей нужен place_id.")

        response_data = await self._get_json(
            path="/v2/place-details",
            params={
                "id": normalized_place_id,
                "features": "details",
                "lang": "ru",
            },
        )

        try:
            response = GeoapifyPlaceDetailsResponse.model_validate(response_data)

        except ValidationError as error:
            raise GeoapifyServiceError(
                "Сервис туристических данных вернул некорректный ответ."
            ) from error

        details_feature = next(
            (
                feature
                for feature in response.features
                if feature.properties.feature_type == "details"
            ),
            None,
        )

        if details_feature is None:
            raise GeoapifyServiceError(
                "Сервис туристических данных не вернул сведения о месте."
            )

        properties = details_feature.properties

        return PlaceDetails(
            # Сохраняем ID исходного поискового кандидата.
            # Вложенный details-объект Geoapify может иметь
            # другой place_id для того же физического места.
            source_place_id=normalized_place_id,
            website=_normalize_website(properties.website),
            opening_hours=_normalize_optional_text(properties.opening_hours),
        )
