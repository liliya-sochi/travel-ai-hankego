"""
Проверка подключения приложения к Redis.

Файл нужен только для ручной диагностики.
Он не запускается вместе с FastAPI или Telegram-ботом.
"""

import asyncio

from redis.asyncio import Redis

from app.config import get_settings


async def check_redis_connection() -> None:
    """
    Подключается к Redis и выполняет команду PING.
    """

    settings = get_settings()

    # Создаём асинхронный Redis-клиент из адреса REDIS_URL.
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    try:
        response = await redis.ping()
        print(f"Redis доступен: {response}")

    finally:
        # Асинхронное соединение нужно закрывать явно.
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(check_redis_connection())
