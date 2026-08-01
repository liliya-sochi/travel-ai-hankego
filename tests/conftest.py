"""
Общие pytest-фикстуры проекта HankeGo.
"""

import os
from collections.abc import AsyncIterator

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


def get_test_database_url() -> str:
    """
    Возвращает адрес только явно указанной тестовой базы.
    """

    database_url = os.getenv(
        "TEST_DATABASE_URL"
    )

    if database_url is None:
        pytest.skip(
            "TEST_DATABASE_URL не задан: "
            "integration-тесты PostgreSQL пропущены."
        )

    parsed_url = make_url(database_url)
    database_name = parsed_url.database or ""

    # Защищает production-базу от случайного TRUNCATE.
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

    Перед и после теста очищает таблицы приложения.
    """

    database_url = get_test_database_url()

    # NullPool не переиспользует соединения между event loop.
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
                text(
                    "TRUNCATE TABLE trips, users "
                    "RESTART IDENTITY CASCADE"
                )
            )

    await clear_database()

    try:
        async with test_session_factory() as session:
            yield session

            # Восстанавливает Session после неуспешного теста.
            await session.rollback()

    finally:
        await clear_database()
        await test_engine.dispose()