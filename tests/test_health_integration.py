"""
Integration-тест системной readiness-проверки.
"""

import os
from urllib.parse import urlparse

import pytest
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.services.health import HealthService


pytestmark = pytest.mark.integration


def get_safe_test_database_url() -> str:
    """
    Возвращает адрес только тестовой PostgreSQL.
    """

    database_url = os.getenv(
        "TEST_DATABASE_URL"
    )

    if database_url is None:
        pytest.skip(
            "TEST_DATABASE_URL не задан."
        )

    parsed_url = make_url(database_url)

    if not (
        parsed_url.database or ""
    ).endswith("_test"):
        raise RuntimeError(
            "Health integration-тест разрешён "
            "только для базы с суффиксом '_test'."
        )

    return database_url


def get_safe_test_redis_url() -> str:
    """
    Возвращает адрес только локального Redis DB 15.
    """

    redis_url = os.getenv(
        "TEST_REDIS_URL"
    )

    if redis_url is None:
        pytest.skip(
            "TEST_REDIS_URL не задан."
        )

    parsed_url = urlparse(redis_url)

    if parsed_url.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise RuntimeError(
            "Health integration-тест разрешён "
            "только для локального Redis."
        )

    if parsed_url.path != "/15":
        raise RuntimeError(
            "Health integration-тест разрешён "
            "только для Redis database 15."
        )

    return redis_url


@pytest.mark.asyncio
async def test_health_service_with_real_dependencies() -> None:
    """
    Проверяет readiness на настоящих PostgreSQL и Redis.
    """

    database_engine = create_async_engine(
        get_safe_test_database_url(),
        poolclass=NullPool,
    )

    redis_client = Redis.from_url(
        get_safe_test_redis_url(),
        decode_responses=True,
    )

    health_service = HealthService(
        database_engine=database_engine,
        redis_client=redis_client,
        timeout_seconds=5.0,
    )

    try:
        readiness = (
            await health_service.check_readiness()
        )

        assert readiness.status == "ready"
        assert readiness.checks.postgresql == "up"
        assert readiness.checks.redis == "up"

    finally:
        await redis_client.aclose()
        await database_engine.dispose()