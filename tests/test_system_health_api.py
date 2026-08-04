"""
Тесты системных HTTP-endpoint.
"""

from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_health_service
from app.main import app
from app.schemas.system import (
    DependencyChecks,
    ReadinessResponse,
)


class FakeHealthService:
    """
    Поддельный сервис проверки инфраструктуры.
    """

    def __init__(
        self,
        readiness: ReadinessResponse,
    ) -> None:
        self._readiness = readiness

    async def check_readiness(
        self,
    ) -> ReadinessResponse:
        """
        Возвращает заранее подготовленный ответ.
        """

        return self._readiness


@pytest.fixture
def clear_health_override() -> Iterator[None]:
    """
    Удаляет подмену HealthService после теста.
    """

    yield

    app.dependency_overrides.pop(
        get_health_service,
        None,
    )


@pytest.mark.asyncio
async def test_liveness_endpoint() -> None:
    """
    Проверяет liveness без инфраструктурных запросов.
    """

    transport = ASGITransport(
        app=app,
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health/live"
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "alive"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "readiness",
        "expected_status_code",
    ),
    [
        (
            ReadinessResponse(
                status="ready",
                checks=DependencyChecks(
                    postgresql="up",
                    redis="up",
                ),
            ),
            200,
        ),
        (
            ReadinessResponse(
                status="not_ready",
                checks=DependencyChecks(
                    postgresql="up",
                    redis="down",
                ),
            ),
            503,
        ),
    ],
    ids=[
        "ready",
        "not-ready",
    ],
)
async def test_readiness_endpoint(
    readiness: ReadinessResponse,
    expected_status_code: int,
    clear_health_override: None,
) -> None:
    """
    Проверяет HTTP-код readiness endpoint.
    """

    app.dependency_overrides[
        get_health_service
    ] = lambda: FakeHealthService(
        readiness=readiness,
    )

    transport = ASGITransport(
        app=app,
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health/ready"
        )

    assert (
        response.status_code
        == expected_status_code
    )

    assert response.json() == (
        readiness.model_dump()
    )