"""
Unit-тесты блокировки генерации маршрутов.
"""

from typing import Any

import pytest
from redis.exceptions import RedisError

from app.services.trip_lock import (
    TripGenerationInProgressError,
    TripGenerationLock,
    TripGenerationLockUnavailableError,
)


class FakeRedisLockClient:
    """
    Поддельный Redis-клиент.
    """

    def __init__(
        self,
        set_result: Any = True,
        set_error: RedisError | None = None,
    ) -> None:
        self._set_result = set_result
        self._set_error = set_error

        self.set_calls: list[tuple[Any, ...]] = []
        self.eval_calls: list[tuple[Any, ...]] = []

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> Any:
        """
        Имитирует создание Redis lock.
        """

        self.set_calls.append(
            (name, value, ex, nx)
        )

        if self._set_error is not None:
            raise self._set_error

        return self._set_result

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> int:
        """
        Имитирует безопасное освобождение lock.
        """

        self.eval_calls.append(
            (
                script,
                numkeys,
                *keys_and_args,
            )
        )

        return 1


@pytest.mark.asyncio
async def test_lock_acquires_and_releases() -> None:
    """
    Проверяет создание и освобождение блокировки.
    """

    redis_client = FakeRedisLockClient()

    generation_lock = TripGenerationLock(
        redis_client=redis_client,
        ttl_seconds=180,
    )

    async with generation_lock.hold(
        telegram_id=9000000001,
    ):
        assert len(redis_client.set_calls) == 1

    assert len(redis_client.eval_calls) == 1

    set_call = redis_client.set_calls[0]
    eval_call = redis_client.eval_calls[0]

    lock_key = set_call[0]
    lock_token = set_call[1]

    assert set_call[2] == 180
    assert set_call[3] is True

    # При освобождении передаются тот же ключ и token.
    assert eval_call[2] == lock_key
    assert eval_call[3] == lock_token


@pytest.mark.asyncio
async def test_lock_rejects_parallel_generation() -> None:
    """
    Проверяет отказ при существующей блокировке.
    """

    redis_client = FakeRedisLockClient(
        set_result=None,
    )

    generation_lock = TripGenerationLock(
        redis_client=redis_client,
        ttl_seconds=180,
    )

    with pytest.raises(
        TripGenerationInProgressError
    ):
        async with generation_lock.hold(
            telegram_id=9000000001,
        ):
            pass

    assert redis_client.eval_calls == []


@pytest.mark.asyncio
async def test_lock_handles_redis_error() -> None:
    """
    Проверяет безопасную обработку ошибки Redis.
    """

    redis_client = FakeRedisLockClient(
        set_error=RedisError(
            "Redis connection failed."
        ),
    )

    generation_lock = TripGenerationLock(
        redis_client=redis_client,
        ttl_seconds=180,
    )

    with pytest.raises(
        TripGenerationLockUnavailableError
    ):
        async with generation_lock.hold(
            telegram_id=9000000001,
        ):
            pass