"""Тесты correlation ID в цепочке Telegram-бота."""

from typing import Any
from uuid import UUID

import httpx
import pytest
from aiogram.types import TelegramObject

import app.bot.api_client as api_client
from app.bot.middleware import CorrelationIdMiddleware
from app.core.request_context import (
    CORRELATION_ID_HEADER,
    get_correlation_id,
)


class FakeAsyncClient:
    """Перехватывает HTTP-запрос без обращения к backend."""

    sent_headers: dict[str, str] = {}

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(
        self,
        *,
        method: str,
        url: str,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        """Сохраняет заголовки и возвращает успешный JSON."""

        type(self).sent_headers = headers

        return httpx.Response(
            status_code=200,
            json={"status": "ok"},
            request=httpx.Request(method, url),
        )


async def request_test_endpoint() -> dict[str, Any]:
    """Вызывает общий HTTP-клиент с тестовыми аргументами."""

    return await api_client._request_backend(
        method="GET",
        path="/test",
        payload={},
        timeout=1.0,
        timeout_message="timeout",
        default_error_message="error",
    )


@pytest.mark.asyncio
async def test_telegram_update_forwards_one_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет передачу одного ID через middleware и HTTP-клиент."""

    monkeypatch.setattr(
        api_client.httpx,
        "AsyncClient",
        FakeAsyncClient,
    )
    observed_id: str | None = None

    async def handler(
        _: TelegramObject,
        __: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal observed_id
        observed_id = get_correlation_id()

        return await request_test_endpoint()

    result = await CorrelationIdMiddleware()(
        handler,
        TelegramObject(),
        {},
    )

    sent_id = FakeAsyncClient.sent_headers[
        CORRELATION_ID_HEADER
    ]

    assert result == {"status": "ok"}
    assert sent_id == observed_id
    assert str(UUID(sent_id)) == sent_id
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_direct_client_cleans_fallback_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет резервный ID при вызове клиента вне middleware."""

    monkeypatch.setattr(
        api_client.httpx,
        "AsyncClient",
        FakeAsyncClient,
    )

    await request_test_endpoint()

    sent_id = FakeAsyncClient.sent_headers[
        CORRELATION_ID_HEADER
    ]

    assert str(UUID(sent_id)) == sent_id
    assert get_correlation_id() is None