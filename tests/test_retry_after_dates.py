"""Проверки Retry-After с фиксированными часами и без реального ожидания."""

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock

import httpx
import pytest

import app.services.ai as ai

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("8", 8.0),
        ("0.25", 0.25),
        ("-1", None),
        ("NaN", None),
        ("Infinity", None),
        ("", None),
        ("PRIVATE_INVALID_HEADER", None),
        ("Tue, 24 Nov 2026 12:00:08 GMT", 8.0),
        ("Tue, 24 Nov 2026 12:00:00 GMT", 0.0),
        ("Tue, 24 Nov 2026 11:59:00 GMT", 0.0),
        ("Tuesday, 24-Nov-26 12:00:08 GMT", 8.0),
        ("Tue Nov 24 12:00:08 2026", 8.0),
        ("Tue, 24 Nov 2026 15:00:08 +0300", 8.0),
        ("Tue, 99 Nov 2026 12:00:08 GMT", None),
        ("Tue, 24 Nov 99999 12:00:08 GMT", None),
    ],
)
def test_retry_after_seconds_and_dates(
    header: str | None, expected: float | None
) -> None:
    """Сохраняет числовой формат, разбирает даты и отбрасывает неверные значения."""
    headers = {"retry-after": header} if header is not None else {}
    response = httpx.Response(429, headers=headers)
    assert ai._parse_retry_after_seconds(response, now=NOW) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [8, 120])
async def test_http_date_respects_provider_wait_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, delay: int
) -> None:
    """Короткую задержку ждёт; длинная не вызывает новый запрос или sleep."""
    parser = ai._parse_retry_after_seconds
    monkeypatch.setattr(
        ai, "_parse_retry_after_seconds", lambda response: parser(response, now=NOW)
    )
    sleep = AsyncMock()
    monkeypatch.setattr(ai.asyncio, "sleep", sleep)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={
                    "retry-after": format_datetime(
                        NOW + timedelta(seconds=delay), usegmt=True
                    )
                },
                text="PRIVATE_PROVIDER_BODY",
            )
        return httpx.Response(200, json={"choices": []})

    with caplog.at_level(logging.INFO, logger=ai.__name__):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            arguments = dict(
                client=client,
                url="https://example.invalid/chat/completions",
                headers={},
                payload={},
                model="test-model",
                attempt=1,
            )
            if delay == 120:
                with pytest.raises(ai.AIProviderRateLimitError) as error:
                    await ai._request_model_with_retry(**arguments)
                assert error.value.retry_after_seconds == 120.0
                assert len(requests) == 1
                sleep.assert_not_awaited()
            else:
                result = await ai._request_model_with_retry(**arguments)
                assert result.data == {"choices": []}
                assert len(requests) == 2
                sleep.assert_awaited_once_with(8.0)

    events = [
        json.loads(record.args[0])
        for record in caplog.records
        if record.msg == "LLM call | %s"
    ]
    assert events[0]["retry_after_ms"] == delay * 1000
    assert "PRIVATE_PROVIDER_BODY" not in caplog.text
