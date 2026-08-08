"""
Подключение FastAPI к Redis.
"""

from fastapi import Request
from redis.asyncio import Redis


def create_redis_client(
    redis_url: str,
) -> Redis:
    """
    Создаёт асинхронный Redis-клиент.
    """

    return Redis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


def get_redis_client(
    request: Request,
) -> Redis:
    """
    Возвращает Redis-клиент текущего FastAPI-приложения.
    """

    redis_client: Redis | None = getattr(
        request.app.state,
        "redis_client",
        None,
    )

    if redis_client is None:
        raise RuntimeError("Redis client is not initialized.")

    return redis_client
