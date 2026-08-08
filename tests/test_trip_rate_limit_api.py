"""
Тест HTTP-ответа при превышении rate limit.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import (
    get_trip_plan_rate_limiter,
)
from app.database import get_session
from app.main import app
from app.services.rate_limit import (
    RateLimitExceededError,
)


class BlockingTripPlanRateLimiter:
    """
    Всегда блокирует генерацию маршрута.
    """

    async def check(
        self,
        telegram_id: int,
    ) -> None:
        """
        Имитирует исчерпанный лимит.
        """

        raise RateLimitExceededError(
            retry_after_seconds=125,
        )


@pytest.fixture
def override_dependencies() -> Iterator[None]:
    """
    Подменяет PostgreSQL и rate limiter.
    """

    async def fake_get_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_session] = fake_get_session

    app.dependency_overrides[get_trip_plan_rate_limiter] = lambda: (
        BlockingTripPlanRateLimiter()
    )

    yield

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_trip_plan_returns_429(
    override_dependencies: None,
) -> None:
    """
    Проверяет HTTP 429 и заголовок Retry-After.
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
                "preferences": {
                    "destination": "Стамбул",
                    "duration_days": 3,
                    "budget": "150000 ₽",
                    "interests": "Архитектура и еда",
                },
            },
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "125"

    assert response.json() == {
        "detail": (
            "Лимит генерации маршрутов исчерпан. Попробуйте снова примерно через 3 мин."
        )
    }
