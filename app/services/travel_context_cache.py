"""Кеширование проверенного туристического контекста в Redis."""

import json
import logging
from hashlib import sha256
from typing import Any, Protocol

from pydantic import ValidationError
from redis.exceptions import RedisError

from app.schemas.geoapify import TravelContext

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "cache:travel-context:v5"


class RedisCacheClient(Protocol):
    """Минимальный Redis-интерфейс для кеша TravelContext."""

    async def get(
        self,
        name: str,
    ) -> str | None:
        """Получает значение по ключу."""

        ...

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
    ) -> Any:
        """Сохраняет значение с ограниченным временем жизни."""

        ...

    async def delete(
        self,
        *names: str,
    ) -> Any:
        """Удаляет значения по ключам."""

        ...


def build_travel_context_cache_key(
    *,
    destination: str,
    categories: list[str],
) -> str:
    """Создаёт стабильный ключ без открытых пользовательских данных."""

    key_payload = {
        "destination": " ".join(destination.casefold().split()),
        "categories": sorted(categories),
    }

    serialized_payload = json.dumps(
        key_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    payload_hash = sha256(serialized_payload.encode("utf-8")).hexdigest()

    return f"{CACHE_KEY_PREFIX}:{payload_hash}"


class RedisTravelContextCache:
    """Хранит сериализованный TravelContext в Redis."""

    def __init__(
        self,
        redis_client: RedisCacheClient,
        ttl_seconds: int,
    ) -> None:
        """Получает Redis-клиент и срок хранения кеша."""

        if ttl_seconds <= 0:
            raise ValueError("Travel context cache TTL must be positive.")

        self._redis_client = redis_client
        self._ttl_seconds = ttl_seconds

    async def get(
        self,
        *,
        destination: str,
        categories: list[str],
    ) -> TravelContext | None:
        """Возвращает валидный контекст или сообщает о промахе кеша."""

        cache_key = build_travel_context_cache_key(
            destination=destination,
            categories=categories,
        )

        try:
            cached_json = await self._redis_client.get(cache_key)

        except RedisError:
            # Кеш является оптимизацией и не должен
            # останавливать создание маршрута.
            logger.warning("Failed to read travel context cache")
            return None

        if cached_json is None:
            return None

        try:
            return TravelContext.model_validate_json(cached_json)

        except ValidationError:
            # Повреждённые или устаревшие данные нельзя передавать LLM.
            logger.warning("Ignored invalid travel context cache entry")

            await self._delete_invalid_entry(
                cache_key=cache_key,
            )

            return None

    async def set(
        self,
        *,
        destination: str,
        categories: list[str],
        context: TravelContext,
    ) -> None:
        """Сохраняет проверенный туристический контекст."""

        cache_key = build_travel_context_cache_key(
            destination=destination,
            categories=categories,
        )

        try:
            await self._redis_client.set(
                cache_key,
                context.model_dump_json(),
                ex=self._ttl_seconds,
            )

        except RedisError:
            # Ошибка записи не должна отменять уже полученные
            # актуальные данные Geoapify.
            logger.warning("Failed to write travel context cache")

    async def _delete_invalid_entry(
        self,
        *,
        cache_key: str,
    ) -> None:
        """Удаляет повреждённую запись, если Redis доступен."""

        try:
            await self._redis_client.delete(cache_key)

        except RedisError:
            logger.warning("Failed to delete invalid travel context cache entry")
