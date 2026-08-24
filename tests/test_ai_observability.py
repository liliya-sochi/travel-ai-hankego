"""Unit-тесты безопасной наблюдаемости LLM."""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.services.ai as ai_service
from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    TravelContext,
)
from app.schemas.trip import TripPreferences
from app.services.ai import (
    AIProviderRateLimitError,
    LLMProviderResponse,
    _extract_llm_response_metadata,
    _request_model,
    _request_model_with_retry,
    generate_trip_plan,
)

PRIVATE_INTERESTS = "PRIVATE_INTERESTS_DO_NOT_LOG"
PRIVATE_ROUTE = "PRIVATE_ROUTE_DO_NOT_LOG"
PRIVATE_BUDGET = "PRIVATE_BUDGET_DO_NOT_LOG"
PRIVATE_API_KEY = "PRIVATE_API_KEY_DO_NOT_LOG"


class DummyAsyncClient:
    """Подменяет AsyncClient, когда HTTP-вызов мокируется отдельно."""

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def build_private_preferences() -> TripPreferences:
    """Создаёт приватные параметры поездки для проверки логов."""

    return TripPreferences(
        destination=PRIVATE_ROUTE,
        duration_days=1,
        budget=PRIVATE_BUDGET,
        interests=PRIVATE_INTERESTS,
    )


def build_private_travel_context() -> TravelContext:
    """Создаёт приватный grounded-контекст для тестов логов."""

    return TravelContext(
        location=DestinationLocation(
            formatted_name=PRIVATE_ROUTE,
            latitude=41.0082,
            longitude=28.9784,
            source_place_id="private-destination-id",
        ),
        requested_categories=[
            "tourism.sights",
        ],
        places=[
            PlaceCandidate(
                name="PRIVATE_PLACE_DO_NOT_LOG",
                formatted_address=("PRIVATE_ADDRESS_DO_NOT_LOG"),
                latitude=41.0086,
                longitude=28.9802,
                categories=["tourism.sights"],
                source_place_id="private-place-id",
            )
        ],
        fetched_at=datetime.now(UTC),
    )


def build_response_data(
    *,
    day_number: int,
    request_id: str,
    prompt_tokens: int,
) -> dict[str, Any]:
    """Создаёт ответ Groq с валидным или неверным номером дня."""

    trip_data = {
        "destination": PRIVATE_ROUTE,
        "duration_days": 1,
        "summary": "Безопасный тестовый маршрут.",
        "days": [
            {
                "day": day_number,
                "title": "Первый день",
                "morning": [
                    {
                        "source_place_id": None,
                        "place_name": None,
                        "description": "Прогулка.",
                    }
                ],
                "afternoon": [
                    {
                        "source_place_id": ("private-place-id"),
                        "place_name": ("PRIVATE_PLACE_DO_NOT_LOG"),
                        "description": "Осмотреть место.",
                    }
                ],
                "evening": [
                    {
                        "source_place_id": None,
                        "place_name": None,
                        "description": "Ужин.",
                    }
                ],
            }
        ],
    }
    return {
        "model": "openai/gpt-oss-120b",
        "choices": [
            {
                "message": {"content": json.dumps(trip_data)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 20,
            "total_tokens": prompt_tokens + 20,
        },
        "x_groq": {"id": request_id},
    }


def read_llm_events(
    caplog: pytest.LogCaptureFixture,
) -> list[dict[str, object]]:
    """Извлекает JSON-события LLM из перехваченных логов."""

    prefix = "LLM call | "
    return [
        json.loads(record.getMessage().removeprefix(prefix))
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


def test_extracts_metadata_and_uses_safe_fallbacks() -> None:
    """Проверяет поля Groq и безопасную обработку неверных метрик."""

    metadata = _extract_llm_response_metadata(
        build_response_data(
            day_number=1,
            request_id="req_success",
            prompt_tokens=15,
        ),
        requested_model="fallback-model",
        fallback_request_id="header-request-id",
    )
    assert metadata.model == "openai/gpt-oss-120b"
    assert metadata.request_id == "req_success"
    assert metadata.prompt_tokens == 15
    assert metadata.completion_tokens == 20
    assert metadata.total_tokens == 35
    assert metadata.finish_reason == "stop"

    fallback_metadata = _extract_llm_response_metadata(
        {
            "model": None,
            "choices": "invalid",
            "usage": {"prompt_tokens": True, "completion_tokens": -1},
            "x_groq": None,
        },
        requested_model="configured-model",
        fallback_request_id="req_from_header",
    )
    assert fallback_metadata.model == "configured-model"
    assert fallback_metadata.request_id == "req_from_header"
    assert fallback_metadata.prompt_tokens is None
    assert fallback_metadata.completion_tokens is None
    assert fallback_metadata.total_tokens is None
    assert fallback_metadata.finish_reason == "unknown"


@pytest.mark.asyncio
async def test_logs_attempts_and_success_without_private_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверяет retry, успешный лог и отсутствие приватных данных."""

    responses = [
        build_response_data(
            day_number=2,
            request_id="req_attempt_1",
            prompt_tokens=10,
        ),
        build_response_data(
            day_number=1,
            request_id="req_attempt_2",
            prompt_tokens=30,
        ),
    ]

    async def fake_request_model(**_: object) -> LLMProviderResponse:
        return LLMProviderResponse(
            data=responses.pop(0),
            duration_ms=125,
            header_request_id=None,
        )

    settings = SimpleNamespace(
        llm_base_url="https://api.groq.com/openai/v1",
        llm_api_key=PRIVATE_API_KEY,
        llm_model="openai/gpt-oss-120b",
    )
    monkeypatch.setattr(ai_service, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_service, "_request_model", fake_request_model)
    monkeypatch.setattr(ai_service.httpx, "AsyncClient", DummyAsyncClient)

    with caplog.at_level(logging.INFO, logger=ai_service.__name__):
        trip_plan = await generate_trip_plan(
            preferences=build_private_preferences(),
            travel_context=build_private_travel_context(),
        )

    events = read_llm_events(caplog)
    assert trip_plan.destination == PRIVATE_ROUTE
    assert [event["attempt"] for event in events] == [1, 2]
    assert [event["outcome"] for event in events] == [
        "semantic_validation_failed",
        "success",
    ]
    assert events[1]["model"] == "openai/gpt-oss-120b"
    assert events[1]["request_id"] == "req_attempt_2"
    assert events[1]["prompt_tokens"] == 30
    assert events[1]["completion_tokens"] == 20
    assert events[1]["total_tokens"] == 50
    assert events[1]["finish_reason"] == "stop"
    assert events[1]["duration_ms"] == 125

    service_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == ai_service.__name__
    )
    assert PRIVATE_INTERESTS not in service_logs
    assert PRIVATE_ROUTE not in service_logs
    assert PRIVATE_API_KEY not in service_logs
    assert PRIVATE_BUDGET not in service_logs
    assert "PRIVATE_PLACE_DO_NOT_LOG" not in service_logs
    assert "PRIVATE_ADDRESS_DO_NOT_LOG" not in service_logs


@pytest.mark.asyncio
async def test_logs_rate_limit_without_sensitive_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверяет безопасный лог provider rate limit."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            headers={
                "x-request-id": "req_rate_limit",
                "retry-after": "7.5",
            },
            json={
                "error": {
                    "message": "PRIVATE_ERROR_BODY_DO_NOT_LOG",
                }
            },
        )

    with caplog.at_level(
        logging.WARNING,
        logger=ai_service.__name__,
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(
                AIProviderRateLimitError,
            ) as error_info:
                await _request_model(
                    client=client,
                    url=("https://api.groq.com/openai/v1/chat/completions"),
                    headers={"Authorization": (f"Bearer {PRIVATE_API_KEY}")},
                    payload={
                        "messages": [
                            {
                                "role": "user",
                                "content": PRIVATE_INTERESTS,
                            }
                        ]
                    },
                    model="openai/gpt-oss-120b",
                    attempt=1,
                )

    assert error_info.value.retry_after_seconds == 7.5

    events = read_llm_events(caplog)

    assert len(events) == 1
    assert events[0]["outcome"] == "rate_limited"
    assert events[0]["http_status"] == 429
    assert events[0]["request_id"] == "req_rate_limit"
    assert events[0]["attempt"] == 1
    assert events[0]["provider_attempt"] == 1
    assert events[0]["max_provider_attempts"] == 2

    service_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == ai_service.__name__
    )

    assert PRIVATE_INTERESTS not in service_logs
    assert PRIVATE_API_KEY not in service_logs
    assert "PRIVATE_ERROR_BODY_DO_NOT_LOG" not in service_logs


@pytest.mark.asyncio
async def test_retries_provider_request_after_short_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторяет provider-запрос после короткого Retry-After."""

    provider_attempts: list[int] = []
    sleep_delays: list[float] = []

    async def fake_request_model(
        **kwargs: Any,
    ) -> LLMProviderResponse:
        provider_attempt = int(kwargs["provider_attempt"])
        provider_attempts.append(provider_attempt)

        if provider_attempt == 1:
            raise AIProviderRateLimitError(
                retry_after_seconds=0.25,
            )

        return LLMProviderResponse(
            data={"result": "success"},
            duration_ms=10,
            header_request_id="req_retry_success",
            provider_attempt=provider_attempt,
        )

    async def fake_sleep(
        delay_seconds: float,
    ) -> None:
        sleep_delays.append(delay_seconds)

    monkeypatch.setattr(
        ai_service,
        "_request_model",
        fake_request_model,
    )
    monkeypatch.setattr(
        ai_service.asyncio,
        "sleep",
        fake_sleep,
    )

    async with httpx.AsyncClient() as client:
        response = await _request_model_with_retry(
            client=client,
            url="https://example.test/chat/completions",
            headers={},
            payload={},
            model="test-model",
            attempt=1,
        )

    assert provider_attempts == [1, 2]
    assert sleep_delays == [0.25]
    assert response.data == {"result": "success"}
    assert response.provider_attempt == 2


@pytest.mark.asyncio
async def test_does_not_retry_after_long_provider_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не удерживает запрос при слишком большом Retry-After."""

    provider_attempts: list[int] = []

    async def fake_request_model(
        **kwargs: Any,
    ) -> LLMProviderResponse:
        provider_attempts.append(
            int(kwargs["provider_attempt"]),
        )
        raise AIProviderRateLimitError(
            retry_after_seconds=30.0,
        )

    async def unexpected_sleep(
        _: float,
    ) -> None:
        pytest.fail("asyncio.sleep не должен вызываться для длинного Retry-After.")

    monkeypatch.setattr(
        ai_service,
        "_request_model",
        fake_request_model,
    )
    monkeypatch.setattr(
        ai_service.asyncio,
        "sleep",
        unexpected_sleep,
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(
            AIProviderRateLimitError,
        ):
            await _request_model_with_retry(
                client=client,
                url="https://example.test/chat/completions",
                headers={},
                payload={},
                model="test-model",
                attempt=1,
            )

    assert provider_attempts == [1]
