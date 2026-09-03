"""Точечный fallback часов работы через Google Places API (New)."""

import math
import re
import unicodedata
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Protocol

import httpx
from pydantic import SecretStr, ValidationError
from redis.exceptions import RedisError

from app.schemas.geoapify import PlaceCandidate
from app.schemas.google_places import (
    GoogleOpeningHours,
    GoogleOpeningPeriod,
    GooglePlace,
    GoogleTextSearchResponse,
)

GOOGLE_TEXT_SEARCH_PATH = "/v1/places:searchText"
GOOGLE_TEXT_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.location,places.regularOpeningHours"
)
GOOGLE_SEARCH_RADIUS_METERS = 1_000.0
GOOGLE_MATCH_DISTANCE_METERS = 1_500.0
GOOGLE_TRANSLATED_MATCH_DISTANCE_METERS = 250.0
GOOGLE_MATCH_NAME_SIMILARITY = 0.6
EARTH_RADIUS_METERS = 6_371_000.0

GOOGLE_MONTHLY_BUDGET_SCRIPT = """
local current = redis.call("INCR", KEYS[1])

if current == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end

return current
"""

_DAY_CODES = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")
_NAME_CHARACTER_PATTERN = re.compile(r"[^\w]+", flags=re.UNICODE)


class GooglePlacesServiceError(Exception):
    """Безопасная ошибка Google Places без внутренних подробностей."""


class GooglePlacesBudgetUnavailableError(Exception):
    """Redis не смог безопасно ограничить платные запросы."""


class RedisBudgetClient(Protocol):
    """Минимальный Redis-интерфейс для месячного счётчика."""

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> Any:
        """Атомарно выполняет Lua-скрипт."""


def _normalize_name(value: str) -> str:
    """Нормализует названия перед безопасным сопоставлением провайдеров."""

    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )

    return _NAME_CHARACTER_PATTERN.sub("", without_accents)


def _calculate_distance_meters(
    *,
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> float:
    """Считает расстояние между двумя координатами по сфере."""

    first_latitude_radians = math.radians(first_latitude)
    second_latitude_radians = math.radians(second_latitude)
    latitude_delta = math.radians(second_latitude - first_latitude)
    longitude_delta = math.radians(second_longitude - first_longitude)

    haversine_value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude_radians)
        * math.cos(second_latitude_radians)
        * math.sin(longitude_delta / 2) ** 2
    )

    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(haversine_value))


def _select_matching_place(
    *,
    source_place: PlaceCandidate,
    google_places: list[GooglePlace],
) -> GooglePlace | None:
    """Выбирает только близкий результат с достаточно похожим названием."""

    normalized_source_name = _normalize_name(source_place.name)
    matches: list[tuple[bool, float, float, GooglePlace]] = []

    for result_index, google_place in enumerate(google_places):
        normalized_google_name = _normalize_name(google_place.display_name.text)
        name_similarity = SequenceMatcher(
            None,
            normalized_source_name,
            normalized_google_name,
        ).ratio()
        distance_meters = _calculate_distance_meters(
            first_latitude=source_place.latitude,
            first_longitude=source_place.longitude,
            second_latitude=google_place.location.latitude,
            second_longitude=google_place.location.longitude,
        )

        if distance_meters > GOOGLE_MATCH_DISTANCE_METERS:
            continue

        has_similar_name = name_similarity >= GOOGLE_MATCH_NAME_SIMILARITY
        is_nearby_first_result = (
            result_index == 0
            and distance_meters <= GOOGLE_TRANSLATED_MATCH_DISTANCE_METERS
        )

        if not has_similar_name and not is_nearby_first_result:
            continue

        matches.append(
            (
                not has_similar_name,
                -name_similarity,
                distance_meters,
                google_place,
            )
        )

    if not matches:
        return None

    return min(matches, key=lambda match: match[:3])[3]


def _format_clock(hour: int, minute: int) -> str:
    """Форматирует время в совместимом с OSM виде."""

    return f"{hour:02d}:{minute:02d}"


def _append_period_intervals(
    intervals_by_day: dict[int, list[tuple[int, int]]],
    period: GoogleOpeningPeriod,
) -> bool:
    """Преобразует обычный или ночной Google-период в дневные интервалы."""

    if period.close is None:
        return False

    open_minutes = period.open.hour * 60 + period.open.minute
    close_minutes = period.close.hour * 60 + period.close.minute

    if period.open.day == period.close.day and close_minutes > open_minutes:
        intervals_by_day[period.open.day].append((open_minutes, close_minutes))
        return True

    next_day = (period.open.day + 1) % 7

    if period.close.day != next_day:
        return False

    if open_minutes < 24 * 60:
        intervals_by_day[period.open.day].append((open_minutes, 24 * 60))

    if close_minutes > 0:
        intervals_by_day[period.close.day].append((0, close_minutes))

    return True


def format_google_opening_hours(
    opening_hours: GoogleOpeningHours,
) -> str | None:
    """Преобразует структурированные Google-периоды в недельное расписание."""

    if not opening_hours.periods:
        return None

    if len(opening_hours.periods) == 1:
        only_period = opening_hours.periods[0]

        if (
            only_period.close is None
            and only_period.open.day == 0
            and only_period.open.hour == 0
            and only_period.open.minute == 0
        ):
            return "24/7"

    intervals_by_day: dict[int, list[tuple[int, int]]] = {day: [] for day in range(7)}

    for period in opening_hours.periods:
        if not _append_period_intervals(intervals_by_day, period):
            return None

    segments: list[str] = []

    for day in (1, 2, 3, 4, 5, 6, 0):
        intervals = sorted(set(intervals_by_day[day]))

        if not intervals:
            continue

        formatted_intervals = ",".join(
            f"{_format_clock(start // 60, start % 60)}-"
            f"{_format_clock(end // 60, end % 60)}"
            for start, end in intervals
        )
        segments.append(f"{_DAY_CODES[day]} {formatted_intervals}")

    return "; ".join(segments) or None


class GooglePlacesMonthlyBudget:
    """Не разрешает превысить заданное число Google-поисков в месяц."""

    def __init__(
        self,
        *,
        redis_client: RedisBudgetClient,
        monthly_limit: int,
    ) -> None:
        if monthly_limit <= 0:
            raise ValueError("Google Places monthly limit must be positive.")

        self._redis_client = redis_client
        self._monthly_limit = monthly_limit

    async def try_acquire(self) -> bool:
        """Резервирует один запрос или безопасно запрещает его."""

        now = datetime.now(UTC)
        next_month_year = now.year + (1 if now.month == 12 else 0)
        next_month = 1 if now.month == 12 else now.month + 1
        next_month_start = datetime(next_month_year, next_month, 1, tzinfo=UTC)
        ttl_seconds = max(1, int((next_month_start - now).total_seconds()))
        redis_key = f"budget:google-places:{now:%Y-%m}"

        try:
            result = await self._redis_client.eval(
                GOOGLE_MONTHLY_BUDGET_SCRIPT,
                1,
                redis_key,
                ttl_seconds,
            )
            request_count = int(result)
        except (RedisError, TypeError, ValueError) as error:
            raise GooglePlacesBudgetUnavailableError(
                "Google Places budget limiter is unavailable."
            ) from error

        return request_count <= self._monthly_limit


class GooglePlacesClient:
    """Ищет часы работы только для одного уже известного места."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def get_opening_hours(
        self,
        place: PlaceCandidate,
    ) -> str | None:
        """Возвращает нормализованное расписание совпавшего Google-места."""

        request_body = {
            "textQuery": f"{place.name}, {place.formatted_address}",
            "maxResultCount": 3,
            "locationBias": {
                "circle": {
                    "center": {
                        "latitude": place.latitude,
                        "longitude": place.longitude,
                    },
                    "radius": GOOGLE_SEARCH_RADIUS_METERS,
                }
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key.get_secret_value(),
            "X-Goog-FieldMask": GOOGLE_TEXT_SEARCH_FIELD_MASK,
        }

        try:
            response = await self._client.post(
                f"{self._base_url}{GOOGLE_TEXT_SEARCH_PATH}",
                json=request_body,
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise GooglePlacesServiceError(
                "Сервис резервных расписаний временно не отвечает."
            ) from error
        except httpx.RequestError as error:
            raise GooglePlacesServiceError(
                "Не удалось подключиться к сервису резервных расписаний."
            ) from error

        if response.status_code == 429:
            raise GooglePlacesServiceError(
                "Лимит сервиса резервных расписаний временно исчерпан."
            )

        if not 200 <= response.status_code < 300:
            raise GooglePlacesServiceError("Сервис резервных расписаний вернул ошибку.")

        try:
            response_data = response.json()
            parsed_response = GoogleTextSearchResponse.model_validate(response_data)
        except (ValueError, ValidationError) as error:
            raise GooglePlacesServiceError(
                "Сервис резервных расписаний вернул некорректные данные."
            ) from error

        matched_place = _select_matching_place(
            source_place=place,
            google_places=parsed_response.places,
        )

        if matched_place is None or matched_place.regular_opening_hours is None:
            return None

        return format_google_opening_hours(matched_place.regular_opening_hours)
