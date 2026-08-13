"""
Общие pytest-фикстуры проекта HankeGo.
"""

import os
from collections.abc import (
    AsyncIterator,
    Iterator,
)
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api.dependencies import (
    get_trip_generation_lock,
    get_trip_intake_rate_limiter,
    get_trip_plan_rate_limiter,
)
from app.core.security import verify_internal_api_key
from app.main import app


class NoOpTripPlanRateLimiter:
    """
    Rate limiter, который ничего не запрещает.
    """

    async def check(
        self,
        telegram_id: int,
    ) -> None:
        """
        Разрешает тестовый запрос.
        """


class NoOpTripIntakeRateLimiter:
    """
    Intake rate limiter, который ничего не запрещает.
    """

    async def check(
        self,
        telegram_id: int,
    ) -> None:
        """
        Разрешает тестовый запрос.
        """


class NoOpTripGenerationLock:
    """
    Блокировка, которая всегда разрешает запрос.
    """

    @asynccontextmanager
    async def hold(
        self,
        telegram_id: int,
    ) -> AsyncIterator[None]:
        """
        Имитирует успешный захват блокировки.
        """

        yield


@pytest.fixture(autouse=True)
def bypass_internal_api_auth() -> Iterator[None]:
    """
    Отключает авторизацию в обычных unit-тестах.
    """

    app.dependency_overrides[verify_internal_api_key] = lambda: None

    yield

    app.dependency_overrides.pop(
        verify_internal_api_key,
        None,
    )


@pytest.fixture(autouse=True)
def bypass_trip_plan_rate_limit() -> Iterator[None]:
    """
    Отключает настоящий Redis rate limiter.
    """

    app.dependency_overrides[get_trip_plan_rate_limiter] = lambda: (
        NoOpTripPlanRateLimiter()
    )

    yield

    app.dependency_overrides.pop(
        get_trip_plan_rate_limiter,
        None,
    )


@pytest.fixture(autouse=True)
def bypass_trip_intake_rate_limit() -> Iterator[None]:
    """
    Отключает настоящий Redis rate limiter intake в unit-тестах.
    """

    app.dependency_overrides[get_trip_intake_rate_limiter] = lambda: (
        NoOpTripIntakeRateLimiter()
    )

    yield

    app.dependency_overrides.pop(
        get_trip_intake_rate_limiter,
        None,
    )


@pytest.fixture(autouse=True)
def bypass_trip_generation_lock() -> Iterator[None]:
    """
    Отключает настоящий Redis lock в unit-тестах.
    """

    app.dependency_overrides[get_trip_generation_lock] = lambda: (
        NoOpTripGenerationLock()
    )

    yield

    app.dependency_overrides.pop(
        get_trip_generation_lock,
        None,
    )


@pytest.fixture
def enable_internal_api_auth(
    bypass_internal_api_auth: None,
) -> Iterator[None]:
    """
    Включает настоящую проверку API key.
    """

    app.dependency_overrides.pop(
        verify_internal_api_key,
        None,
    )

    yield


def get_test_database_url() -> str:
    """
    Возвращает адрес только явно указанной тестовой базы.
    """

    database_url = os.getenv("TEST_DATABASE_URL")

    if database_url is None:
        pytest.skip(
            "TEST_DATABASE_URL не задан: integration-тесты PostgreSQL пропущены."
        )

    parsed_url = make_url(database_url)
    database_name = parsed_url.database or ""

    if not database_name.endswith("_test"):
        raise RuntimeError(
            "Integration-тесты разрешены только для базы, "
            "имя которой заканчивается на '_test'."
        )

    return database_url


@pytest_asyncio.fixture
async def database_session() -> AsyncIterator[AsyncSession]:
    """
    Выдаёт изолированную сессию тестовой PostgreSQL.
    """

    database_url = get_test_database_url()

    test_engine = create_async_engine(
        database_url,
        poolclass=NullPool,
    )

    test_session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def clear_database() -> None:
        """
        Очищает прикладные таблицы тестовой базы.
        """

        async with test_engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE trips, users RESTART IDENTITY CASCADE")
            )

    await clear_database()

    try:
        async with test_session_factory() as session:
            yield session

            await session.rollback()

    finally:
        await clear_database()
        await test_engine.dispose()
