"""
Unit-тесты сервиса проверки инфраструктуры.
"""

from typing import Any

import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.services.health import HealthService


class FakeDatabaseResult:
    """
    Поддельный результат SELECT 1.
    """

    def scalar_one(self) -> int:
        """
        Возвращает единицу.
        """

        return 1


class FakeDatabaseConnection:
    """
    Поддельное соединение PostgreSQL.
    """

    def __init__(
        self,
        error: SQLAlchemyError | None = None,
    ) -> None:
        self._error = error

    async def execute(
        self,
        statement: Any,
    ) -> FakeDatabaseResult:
        """
        Имитирует SQL-запрос.
        """

        if self._error is not None:
            raise self._error

        return FakeDatabaseResult()


class FakeDatabaseConnectionContext:
    """
    Асинхронный контекст соединения.
    """

    def __init__(
        self,
        connection: FakeDatabaseConnection,
    ) -> None:
        self._connection = connection

    async def __aenter__(
        self,
    ) -> FakeDatabaseConnection:
        """
        Возвращает тестовое соединение.
        """

        return self._connection

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        """
        Завершает тестовый контекст.
        """


class FakeDatabaseEngine:
    """
    Поддельный SQLAlchemy engine.
    """

    def __init__(
        self,
        error: SQLAlchemyError | None = None,
    ) -> None:
        self._error = error

    def connect(
        self,
    ) -> FakeDatabaseConnectionContext:
        """
        Создаёт тестовый контекст соединения.
        """

        return FakeDatabaseConnectionContext(
            FakeDatabaseConnection(
                error=self._error,
            )
        )


class FakeRedisClient:
    """
    Поддельный Redis-клиент.
    """

    def __init__(
        self,
        error: RedisError | None = None,
    ) -> None:
        self._error = error

    async def ping(self) -> bool:
        """
        Имитирует Redis PING.
        """

        if self._error is not None:
            raise self._error

        return True


@pytest.mark.asyncio
async def test_health_service_reports_ready() -> None:
    """
    Проверяет ответ при доступной инфраструктуре.
    """

    health_service = HealthService(
        database_engine=FakeDatabaseEngine(),  # type: ignore[arg-type]
        redis_client=FakeRedisClient(),  # type: ignore[arg-type]
    )

    readiness = (
        await health_service.check_readiness()
    )

    assert readiness.status == "ready"
    assert readiness.checks.postgresql == "up"
    assert readiness.checks.redis == "up"


@pytest.mark.asyncio
async def test_health_service_reports_postgresql_down() -> None:
    """
    Проверяет недоступность PostgreSQL.
    """

    health_service = HealthService(
        database_engine=FakeDatabaseEngine(
            error=SQLAlchemyError(
                "PostgreSQL unavailable."
            ),
        ),  # type: ignore[arg-type]
        redis_client=FakeRedisClient(),  # type: ignore[arg-type]
    )

    readiness = (
        await health_service.check_readiness()
    )

    assert readiness.status == "not_ready"
    assert readiness.checks.postgresql == "down"
    assert readiness.checks.redis == "up"


@pytest.mark.asyncio
async def test_health_service_reports_redis_down() -> None:
    """
    Проверяет недоступность Redis.
    """

    health_service = HealthService(
        database_engine=FakeDatabaseEngine(),  # type: ignore[arg-type]
        redis_client=FakeRedisClient(
            error=RedisError(
                "Redis unavailable."
            ),
        ),  # type: ignore[arg-type]
    )

    readiness = (
        await health_service.check_readiness()
    )

    assert readiness.status == "not_ready"
    assert readiness.checks.postgresql == "up"
    assert readiness.checks.redis == "down"