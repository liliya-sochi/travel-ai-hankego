"""Тесты correlation ID на стороне FastAPI и логирования."""

import logging
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.logging import CorrelationIdFilter
from app.core.request_context import (
    CORRELATION_ID_HEADER,
    EMPTY_CORRELATION_ID,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from app.main import app


VALID_CORRELATION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_fastapi_preserves_valid_correlation_id() -> None:
    """Проверяет сохранение валидного UUID из входящего запроса."""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health/live",
            headers={
                CORRELATION_ID_HEADER: VALID_CORRELATION_ID,
            },
        )

    assert response.status_code == 200
    assert response.headers[
        CORRELATION_ID_HEADER
    ] == VALID_CORRELATION_ID
    assert get_correlation_id() is None


@pytest.mark.asyncio
async def test_fastapi_replaces_invalid_correlation_id() -> None:
    """Проверяет замену произвольного заголовка новым UUID."""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/health/live",
            headers={CORRELATION_ID_HEADER: "wrong-id"},
        )

    generated_id = response.headers[CORRELATION_ID_HEADER]

    assert generated_id != "wrong-id"
    assert str(UUID(generated_id)) == generated_id
    assert get_correlation_id() is None


def test_logging_filter_adds_current_or_empty_id() -> None:
    """Проверяет ID внутри операции и безопасный fallback вне её."""

    correlation_filter = CorrelationIdFilter()
    inside_record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "inside",
        (),
        None,
    )
    context_token = set_correlation_id(VALID_CORRELATION_ID)

    try:
        assert correlation_filter.filter(inside_record) is True
        assert inside_record.correlation_id == VALID_CORRELATION_ID

    finally:
        reset_correlation_id(context_token)

    outside_record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "outside",
        (),
        None,
    )

    assert correlation_filter.filter(outside_record) is True
    assert outside_record.correlation_id == EMPTY_CORRELATION_ID