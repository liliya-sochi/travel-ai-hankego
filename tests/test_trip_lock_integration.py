"""
Integration-тест Redis lock.
"""

import os
from hashlib import sha256
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.trip_lock import (
    TripGenerationInProgressError,
    TripGenerationLock,
)


pytestmark = pytest.mark.integration


def get_test_redis_url() -> str:
    """
    Возвращает адрес безопасного тестового Redis.
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
async def test_only_one_lock_can_be_held() -> None:
    """
    Проверяет взаимное исключение на настоящем Redis.
    """

    redis_client = Redis.from_url(
        get_test_redis_url(),
        decode_responses=True,
    )

    telegram_id = 9000000001
    key_prefix = f"test:trip-lock:{uuid4().hex}"

    telegram_id_hash = sha256(
        str(telegram_id).encode("utf-8")
    ).hexdigest()

    redis_key = (
        f"{key_prefix}:{telegram_id_hash}"
    )

    first_lock = TripGenerationLock(
        redis_client=redis_client,
        ttl_seconds=60,
        key_prefix=key_prefix,
    )

    second_lock = TripGenerationLock(
        redis_client=redis_client,
        ttl_seconds=60,
        key_prefix=key_prefix,
    )

    try:
        async with first_lock.hold(
            telegram_id=telegram_id,
        ):
            with pytest.raises(
                TripGenerationInProgressError
            ):
                async with second_lock.hold(
                    telegram_id=telegram_id,
                ):
                    pass

        # После освобождения первый lock больше не мешает.
        async with second_lock.hold(
            telegram_id=telegram_id,
        ):
            pass

    finally:
        await redis_client.delete(
            redis_key
        )

        await redis_client.aclose()