"""
Общие зависимости HTTP API HankeGo.
"""

from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis

from app.config import get_settings
from app.core.redis import get_redis_client
from app.database import engine
from app.services.health import HealthService
from app.services.rate_limit import TripPlanRateLimiter
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
