"""
Проверка готовности инфраструктуры HankeGo.
"""

import logging
from asyncio import gather, timeout

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.schemas.system import (
    DependencyChecks,
    ReadinessResponse,
)


logger = logging.getLogger(__name__)


class HealthService:
    """
    Проверяет обязательные зависимости приложения.
    """

    def __init__(
        self,
        database_engine: AsyncEngine,
        redis_client: Redis,
        timeout_seconds: float = 2.0,
    ) -> None:
        """
        Получает существующие клиенты PostgreSQL и Redis.
        """

        self._database_engine = database_engine
        self._redis_client = redis_client
        self._timeout_seconds = timeout_seconds

    async def check_readiness(
        self,
    ) -> ReadinessResponse:
        """
        Параллельно проверяет PostgreSQL и Redis.
        """

        (
            postgresql_is_up,
            redis_is_up,
        ) = await gather(
            self._check_postgresql(),
            self._check_redis(),
        )

        is_ready = (
            postgresql_is_up
            and redis_is_up
        )

        return ReadinessResponse(
            status=(
                "ready"
                if is_ready
                else "not_ready"
            ),
            checks=DependencyChecks(
                postgresql=(
                    "up"
                    if postgresql_is_up
                    else "down"
                ),
                redis=(
                    "up"
                    if redis_is_up
                    else "down"
                ),
            ),
        )

    async def _check_postgresql(
        self,
    ) -> bool:
        """
        Проверяет получение ответа от PostgreSQL.
        """

        try:
            async with timeout(
                self._timeout_seconds
            ):
                async with (
                    self._database_engine.connect()
                    as connection
                ):
                    result = await connection.execute(
                        text("SELECT 1")
                    )

                    return (
                        result.scalar_one()
                        == 1
                    )

        except (
            TimeoutError,
            SQLAlchemyError,
        ) as error:
            logger.warning(
                "PostgreSQL readiness check failed: %s",
                type(error).__name__,
            )

            return False

    async def _check_redis(
        self,
    ) -> bool:
        """
        Проверяет получение PONG от Redis.
        """

        try:
            async with timeout(
                self._timeout_seconds
            ):
                ping_result = (
                    await self._redis_client.ping()
                )

                return ping_result is True

        except (
            TimeoutError,
            RedisError,
        ) as error:
            logger.warning(
                "Redis readiness check failed: %s",
                type(error).__name__,
            )

            return False