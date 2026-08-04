"""
Защита от параллельной генерации маршрутов.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from secrets import token_urlsafe
from typing import Any, Protocol

from redis.exceptions import RedisError


logger = logging.getLogger(__name__)


RELEASE_LOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end

return 0
"""


class RedisLockClient(Protocol):
    """
    Минимальный Redis-интерфейс для блокировки.
    """

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> Any:
        """
        Создаёт блокировку с ограниченным временем жизни.
        """

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: str | int,
    ) -> Any:
        """
        Выполняет Lua-скрипт в Redis.
        """


class TripGenerationInProgressError(Exception):
    """
    У пользователя уже выполняется генерация маршрута.
    """


class TripGenerationLockUnavailableError(Exception):
    """
    Redis-блокировка временно недоступна.
    """


class TripGenerationLock:
    """
    Разрешает только одну генерацию на пользователя.
    """

    def __init__(
        self,
        redis_client: RedisLockClient,
        ttl_seconds: int,
        key_prefix: str = "lock:trip-plan",
    ) -> None:
        """
        Получает Redis и срок действия блокировки.
        """

        self._redis_client = redis_client
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix

    @asynccontextmanager
    async def hold(
        self,
        telegram_id: int,
    ) -> AsyncIterator[None]:
        """
        Захватывает блокировку на время генерации маршрута.
        """

        lock_key = self._build_key(
            telegram_id=telegram_id,
        )

        # Token отличает нашу блокировку
        # от блокировок других процессов.
        lock_token = token_urlsafe(32)

        try:
            acquired = await self._redis_client.set(
                lock_key,
                lock_token,
                ex=self._ttl_seconds,
                nx=True,
            )

        except RedisError as error:
            raise TripGenerationLockUnavailableError(
                "Redis trip generation lock is unavailable."
            ) from error

        if not acquired:
            raise TripGenerationInProgressError(
                "Trip generation is already in progress."
            )

        try:
            yield

        finally:
            await self._release(
                lock_key=lock_key,
                lock_token=lock_token,
            )

    async def _release(
        self,
        lock_key: str,
        lock_token: str,
    ) -> None:
        """
        Удаляет только принадлежащую текущему процессу блокировку.
        """

        try:
            await self._redis_client.eval(
                RELEASE_LOCK_SCRIPT,
                1,
                lock_key,
                lock_token,
            )

        except RedisError:
            # Ошибка удаления не должна превращать
            # успешную генерацию в HTTP 500.
            # Redis удалит ключ автоматически по TTL.
            logger.exception(
                "Failed to release trip generation lock"
            )

    def _build_key(
        self,
        telegram_id: int,
    ) -> str:
        """
        Создаёт Redis-ключ без открытого Telegram ID.
        """

        telegram_id_hash = sha256(
            str(telegram_id).encode("utf-8")
        ).hexdigest()

        return (
            f"{self._key_prefix}:"
            f"{telegram_id_hash}"
        )