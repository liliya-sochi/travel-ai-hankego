"""
Тест HTTP-ответа при параллельной генерации.
"""

from collections.abc import (
    AsyncIterator,
    Iterator,
)
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import (
    get_trip_generation_lock,
)
from app.database import get_session
from app.main import app
from app.services.trip_lock import (
    TripGenerationInProgressError,
)


class BlockingTripGenerationLock:
    """
    Всегда сообщает о текущей генерации.
    """

    @asynccontextmanager
    async def hold(
        self,
        telegram_id: int,
    ) -> AsyncIterator[None]:
        """
        Имитирует уже занятую блокировку.
        """

        raise TripGenerationInProgressError(
            "Trip generation is already in progress."
        )

        yield


@pytest.fixture
def override_dependencies() -> Iterator[None]:
    """
    Подменяет PostgreSQL и Redis lock.
    """

    async def fake_get_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_session] = (
        fake_get_session
    )

    app.dependency_overrides[
        get_trip_generation_lock
    ] = lambda: BlockingTripGenerationLock()

    yield

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_trip_plan_returns_409(
    override_dependencies: None,
) -> None:
    """
    Проверяет отказ при параллельной генерации.
    """

    transport = ASGITransport(
        app=app,
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-plan",
            json={
                "telegram_id": 9000000001,
                "first_name": "Liliya",
                "prompt": (
                    "Хочу провести три дня в Стамбуле."
                ),
            },
        )

    assert response.status_code == 409

    assert response.json() == {
        "detail": (
            "Ваш маршрут уже создаётся. "
            "Дождитесь завершения текущей генерации."
        )
    }