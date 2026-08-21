"""Unit-тесты Redis-кеша туристического контекста."""

from datetime import UTC, datetime

import pytest
from redis.exceptions import RedisError

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    TravelContext,
)
from app.services.travel_context_cache import (
    CACHE_KEY_PREFIX,
    RedisTravelContextCache,
    build_travel_context_cache_key,
)


class FakeRedisCacheClient:
    """Управляемая имитация Redis для unit-тестов."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.deleted_keys: list[str] = []
        self.get_error: RedisError | None = None
        self.set_error: RedisError | None = None

    async def get(
        self,
        name: str,
    ) -> str | None:
        """Возвращает сохранённое значение."""

        if self.get_error is not None:
            raise self.get_error

        return self.values.get(name)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
    ) -> bool:
        """Сохраняет значение вместе с TTL."""

        if self.set_error is not None:
            raise self.set_error

        self.values[name] = value
        self.expirations[name] = ex

        return True

    async def delete(
        self,
        *names: str,
    ) -> int:
        """Удаляет указанные ключи."""

        deleted_count = 0

        for name in names:
            if name in self.values:
                del self.values[name]
                deleted_count += 1

            self.expirations.pop(name, None)
            self.deleted_keys.append(name)

        return deleted_count


def build_context() -> TravelContext:
    """Создаёт тестовый туристический контекст."""

    return TravelContext(
        location=DestinationLocation(
            formatted_name="Стамбул, Турция",
            latitude=41.0082,
            longitude=28.9784,
            source_place_id="istanbul-place-id",
        ),
        requested_categories=[
            "tourism.sights",
            "entertainment.museum",
        ],
        places=[
            PlaceCandidate(
                name="Айя-София",
                formatted_address="Султанахмет, Стамбул",
                latitude=41.0086,
                longitude=28.9802,
                categories=["tourism.sights"],
                source_place_id="hagia-sophia-id",
            )
        ],
        fetched_at=datetime(
            2026,
            8,
            20,
            12,
            0,
            tzinfo=UTC,
        ),
    )


def test_cache_key_is_normalized_and_private() -> None:
    """Проверяет стабильность и приватность Redis-ключа."""

    first_key = build_travel_context_cache_key(
        destination="  СтАМБУЛ  ",
        categories=[
            "tourism.sights",
            "entertainment.museum",
        ],
    )
    second_key = build_travel_context_cache_key(
        destination="стамбул",
        categories=[
            "entertainment.museum",
            "tourism.sights",
        ],
    )

    assert first_key == second_key
    assert first_key.startswith(f"{CACHE_KEY_PREFIX}:")
    assert "стамбул" not in first_key.casefold()


@pytest.mark.asyncio
async def test_saves_and_restores_context_with_ttl() -> None:
    """Проверяет сериализацию, чтение и TTL."""

    redis_client = FakeRedisCacheClient()
    cache = RedisTravelContextCache(
        redis_client=redis_client,
        ttl_seconds=21_600,
    )
    expected_context = build_context()

    await cache.set(
        destination="Стамбул",
        categories=expected_context.requested_categories,
        context=expected_context,
    )

    restored_context = await cache.get(
        destination="Стамбул",
        categories=expected_context.requested_categories,
    )

    cache_key = build_travel_context_cache_key(
        destination="Стамбул",
        categories=expected_context.requested_categories,
    )

    assert restored_context == expected_context
    assert redis_client.expirations[cache_key] == 21_600


@pytest.mark.asyncio
async def test_returns_none_on_cache_miss() -> None:
    """Проверяет обычный cache miss."""

    cache = RedisTravelContextCache(
        redis_client=FakeRedisCacheClient(),
        ttl_seconds=21_600,
    )

    context = await cache.get(
        destination="Стамбул",
        categories=["tourism.sights"],
    )

    assert context is None


@pytest.mark.asyncio
async def test_deletes_invalid_cached_context() -> None:
    """Проверяет удаление повреждённой записи."""

    redis_client = FakeRedisCacheClient()
    cache = RedisTravelContextCache(
        redis_client=redis_client,
        ttl_seconds=21_600,
    )
    cache_key = build_travel_context_cache_key(
        destination="Стамбул",
        categories=["tourism.sights"],
    )
    redis_client.values[cache_key] = "{invalid-json"

    context = await cache.get(
        destination="Стамбул",
        categories=["tourism.sights"],
    )

    assert context is None
    assert cache_key not in redis_client.values
    assert redis_client.deleted_keys == [cache_key]


@pytest.mark.asyncio
async def test_read_error_behaves_as_cache_miss() -> None:
    """Проверяет fail-open при ошибке чтения Redis."""

    redis_client = FakeRedisCacheClient()
    redis_client.get_error = RedisError("Redis read failed.")

    cache = RedisTravelContextCache(
        redis_client=redis_client,
        ttl_seconds=21_600,
    )

    context = await cache.get(
        destination="Стамбул",
        categories=["tourism.sights"],
    )

    assert context is None


@pytest.mark.asyncio
async def test_write_error_does_not_interrupt_request() -> None:
    """Проверяет fail-open при ошибке записи Redis."""

    redis_client = FakeRedisCacheClient()
    redis_client.set_error = RedisError("Redis write failed.")

    cache = RedisTravelContextCache(
        redis_client=redis_client,
        ttl_seconds=21_600,
    )

    await cache.set(
        destination="Стамбул",
        categories=["tourism.sights"],
        context=build_context(),
    )

    assert redis_client.values == {}
