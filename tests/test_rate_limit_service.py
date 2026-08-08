"""
Unit-тесты Redis rate limiter.
"""

from typing import Any

import pytest
from redis.exceptions import RedisError

from app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    TripPlanRateLimiter,
)


class FakeRedisScriptClient:
    """
    Поддельный Redis для проверки Python-логики.
    """

    def __init__(
        self,
        result: Any = None,
        error: RedisError | None = None,
    ) -> None:
        self._result = result
        self._error = error

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> Any:
        """
        Возвращает заранее заданный результат.
        """

        if self._error is not None:
            raise self._error

        return self._result


@pytest.mark.asyncio
async def test_rate_limiter_allows_request() -> None:
    """
    Проверяет запрос внутри лимита.
    """

    redis_client = FakeRedisScriptClient(
        result=[3, 1200],
    )

    rate_limiter = TripPlanRateLimiter(
        redis_client=redis_client,
        limit=10,
        window_seconds=3600,
    )

    await rate_limiter.check(
        telegram_id=9000000001,
    )


@pytest.mark.asyncio
async def test_rate_limiter_rejects_request() -> None:
    """
    Проверяет запрос сверх лимита.
    """

    redis_client = FakeRedisScriptClient(
        result=[11, 725],
    )

    rate_limiter = TripPlanRateLimiter(
        redis_client=redis_client,
        limit=10,
        window_seconds=3600,
    )

    with pytest.raises(RateLimitExceededError) as error_info:
        await rate_limiter.check(
            telegram_id=9000000001,
        )

    assert error_info.value.retry_after_seconds == 725


@pytest.mark.asyncio
async def test_rate_limiter_handles_redis_error() -> None:
    """
    Проверяет безопасную обработку ошибки Redis.
    """

    redis_client = FakeRedisScriptClient(
        error=RedisError("Redis connection failed."),
    )

    rate_limiter = TripPlanRateLimiter(
        redis_client=redis_client,
        limit=10,
        window_seconds=3600,
    )

    with pytest.raises(RateLimitUnavailableError):
        await rate_limiter.check(
            telegram_id=9000000001,
        )
