"""
Integration-тест rate limiter с настоящим Redis.
"""

import os
from hashlib import sha256
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.rate_limit import (
    RateLimitExceededError,
    TripPlanRateLimiter,
)


pytestmark = pytest.mark.integration


def get_test_redis_url() -> str:
    """
    Возвращает адрес только безопасного тестового Redis.
    """

    redis_url = os.getenv(
        "TEST_REDIS_URL"
    )

    if redis_url is None:
        pytest.skip(
            "TEST_REDIS_URL не задан: "
            "Redis integration-тест пропущен."
        )

    parsed_url = urlparse(redis_url)

    if parsed_url.hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        raise RuntimeError(
            "Integration-тест разрешён только "
            "для локального Redis."
        )

    if parsed_url.path != "/15":
        raise RuntimeError(
            "Integration-тест разрешён только "
            "для Redis database 15."
        )

    return redis_url


@pytest.mark.asyncio
async def test_rate_limiter_with_real_redis() -> None:
    """
    Проверяет Lua-скрипт на настоящем Redis.
    """

    redis_url = get_test_redis_url()

    redis_client = Redis.from_url(
        redis_url,
        decode_responses=True,
    )

    telegram_id = 9000000001
    key_prefix = (
        f"test:rate-limit:{uuid4().hex}"
    )

    telegram_id_hash = sha256(
        str(telegram_id).encode("utf-8")
    ).hexdigest()

    redis_key = (
        f"{key_prefix}:{telegram_id_hash}"
    )

    rate_limiter = TripPlanRateLimiter(
        redis_client=redis_client,
        limit=2,
        window_seconds=60,
        key_prefix=key_prefix,
    )

    try:
        await rate_limiter.check(
            telegram_id=telegram_id,
        )

        await rate_limiter.check(
            telegram_id=telegram_id,
        )

        with pytest.raises(
            RateLimitExceededError
        ) as error_info:
            await rate_limiter.check(
                telegram_id=telegram_id,
            )

        assert (
            1
            <= error_info.value.retry_after_seconds
            <= 60
        )

    finally:
        await redis_client.delete(
            redis_key
        )

        await redis_client.aclose()