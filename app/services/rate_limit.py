"""
Ограничение частоты дорогих операций HankeGo.
"""

from hashlib import sha256
from typing import Any, Protocol

from redis.exceptions import RedisError

RATE_LIMIT_SCRIPT = """
local current = redis.call("INCR", KEYS[1])

if current == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end

local ttl = redis.call("TTL", KEYS[1])

return {current, ttl}
"""


class RedisScriptClient(Protocol):
    """
    Минимальный Redis-интерфейс, нужный rate limiter.
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


class RateLimitExceededError(Exception):
    """
    Пользователь превысил допустимое количество запросов.
    """

    def __init__(
        self,
        retry_after_seconds: int,
    ) -> None:
        """
        Сохраняет время до следующей разрешённой попытки.
        """

        self.retry_after_seconds = retry_after_seconds

        super().__init__("Trip plan generation rate limit exceeded.")


class RateLimitUnavailableError(Exception):
    """
    Redis rate limiter временно недоступен.
    """


class TripPlanRateLimiter:
    """
    Ограничивает количество генераций маршрута.
    """

    def __init__(
        self,
        redis_client: RedisScriptClient,
        limit: int,
        window_seconds: int,
        key_prefix: str = "rate-limit:trip-plan",
    ) -> None:
        """
        Получает Redis и параметры ограничения.
        """

        self._redis_client = redis_client
        self._limit = limit
        self._window_seconds = window_seconds
        self._key_prefix = key_prefix

    async def check(
        self,
        telegram_id: int,
    ) -> None:
        """
        Увеличивает счётчик и проверяет лимит пользователя.
        """

        redis_key = self._build_key(telegram_id=telegram_id)

        try:
            result = await self._redis_client.eval(
                RATE_LIMIT_SCRIPT,
                1,
                redis_key,
                self._window_seconds,
            )

        except RedisError as error:
            raise RateLimitUnavailableError(
                "Redis rate limiter is unavailable."
            ) from error

        try:
            request_count = int(result[0])
            ttl_seconds = int(result[1])

        except (
            IndexError,
            TypeError,
            ValueError,
        ) as error:
            raise RateLimitUnavailableError(
                "Redis returned an invalid rate limit result."
            ) from error

        if request_count <= self._limit:
            return

        retry_after_seconds = ttl_seconds if ttl_seconds > 0 else self._window_seconds

        raise RateLimitExceededError(
            retry_after_seconds=retry_after_seconds,
        )

    def _build_key(
        self,
        telegram_id: int,
    ) -> str:
        """
        Создаёт Redis-ключ без открытого Telegram ID.
        """

        telegram_id_hash = sha256(str(telegram_id).encode("utf-8")).hexdigest()

        return f"{self._key_prefix}:{telegram_id_hash}"


class TripIntakeRateLimiter(TripPlanRateLimiter):
    """
    Ограничивает количество LLM-разборов свободных сообщений.

    Использует тот же атомарный Redis-механизм, но отдельный
    префикс ключей, поэтому не расходует лимит генерации маршрутов.
    """
