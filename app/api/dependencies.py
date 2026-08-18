"""
Общие зависимости HTTP API HankeGo.
"""

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends
from redis.asyncio import Redis

from app.config import get_settings
from app.core.redis import get_redis_client
from app.database import engine
from app.services.geoapify import GeoapifyClient
from app.services.health import HealthService
from app.services.rate_limit import (
    TripIntakeRateLimiter,
    TripPlanRateLimiter,
)
from app.services.trip_enrichment import TripEnrichmentService
from app.services.trip_lock import TripGenerationLock

RedisDependency = Annotated[
    Redis,
    Depends(get_redis_client),
]


def get_health_service(
    redis_client: RedisDependency,
) -> HealthService:
    """
    Создаёт сервис проверки инфраструктуры.
    """

    return HealthService(
        database_engine=engine,
        redis_client=redis_client,
    )


def get_trip_plan_rate_limiter(
    redis_client: RedisDependency,
) -> TripPlanRateLimiter:
    """
    Создаёт rate limiter генерации маршрутов.
    """

    settings = get_settings()

    return TripPlanRateLimiter(
        redis_client=redis_client,
        limit=settings.trip_plan_rate_limit,
        window_seconds=(settings.trip_plan_rate_window_seconds),
    )


def get_trip_intake_rate_limiter(
    redis_client: RedisDependency,
) -> TripIntakeRateLimiter:
    """Создаёт отдельный rate limiter диалогового разбора."""

    settings = get_settings()

    return TripIntakeRateLimiter(
        redis_client=redis_client,
        limit=settings.trip_intake_rate_limit,
        window_seconds=settings.trip_intake_rate_window_seconds,
        key_prefix="rate-limit:trip-intake",
    )


def get_trip_generation_lock(
    redis_client: RedisDependency,
) -> TripGenerationLock:
    """
    Создаёт блокировку параллельной генерации.
    """

    settings = get_settings()

    return TripGenerationLock(
        redis_client=redis_client,
        ttl_seconds=(settings.trip_plan_lock_ttl_seconds),
    )


async def get_trip_enrichment_service() -> AsyncIterator[TripEnrichmentService]:
    """
    Создаёт сервис актуальных туристических данных.

    Один AsyncClient переиспользуется для геокодирования
    и поиска мест в рамках одного HTTP-запроса FastAPI.
    """

    settings = get_settings()

    timeout = httpx.Timeout(
        timeout=settings.geoapify_timeout_seconds,
        connect=min(
            5.0,
            settings.geoapify_timeout_seconds,
        ),
    )

    async with httpx.AsyncClient(
        timeout=timeout,
    ) as http_client:
        geoapify_client = GeoapifyClient(
            client=http_client,
            api_key=settings.geoapify_api_key,
            base_url=settings.geoapify_base_url,
        )

        yield TripEnrichmentService(
            places_provider=geoapify_client,
        )
