"""
Тесты внутренней авторизации FastAPI.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "X-Internal-API-Key": "wrong-internal-key",
        },
    ],
    ids=[
        "missing-key",
        "wrong-key",
    ],
)
async def test_protected_api_rejects_invalid_key(
    headers: dict[str, str],
    enable_internal_api_auth: None,
) -> None:
    """
    Проверяет одинаковый отказ без ключа и с неверным ключом.
    """

    transport = ASGITransport(
        app=app,
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-history",
            headers=headers,
            json={
                "telegram_id": 9000000001,
                "limit": 10,
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Ошибка внутренней авторизации."}


@pytest.mark.asyncio
async def test_protected_api_accepts_valid_key(
    enable_internal_api_auth: None,
) -> None:
    """
    Проверяет прохождение security dependency с верным ключом.
    """

    settings = get_settings()

    transport = ASGITransport(
        app=app,
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-history",
            headers={
                "X-Internal-API-Key": (settings.internal_api_key.get_secret_value()),
            },
            # Пустое тело специально вызывает следующую стадию:
            # Pydantic-валидацию после успешной авторизации.
            json={},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_info_endpoint_remains_public(
    enable_internal_api_auth: None,
) -> None:
    """
    Проверяет публичный информационный endpoint без API key.
    """

    transport = ASGITransport(
        app=app,
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get("/info")

    assert response.status_code == 200

    assert response.json() == {
        "name": "HankeGo API",
        "version": "0.1.0",
    }
