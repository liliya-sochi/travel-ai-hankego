"""
Unit-тесты LLM Structured Output для conversational intake.
"""

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.ai as ai_service
from app.schemas.trip import TripDraft
from app.services.ai import (
    AIServiceError,
    LLMProviderResponse,
    analyze_trip_message,
)

PRIVATE_MESSAGE = "PRIVATE_USER_MESSAGE_DO_NOT_LOG"
PRIVATE_API_KEY = "PRIVATE_API_KEY_DO_NOT_LOG"


class DummyAsyncClient:
    """Подменяет AsyncClient, когда HTTP-вызов мокируется."""

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def build_provider_response(
    content: str,
) -> LLMProviderResponse:
    """Создаёт минимальный ответ LLM-провайдера."""

    return LLMProviderResponse(
        data={
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        },
        duration_ms=50,
        header_request_id="request-id",
    )


@pytest.mark.asyncio
async def test_analyze_trip_message_returns_extraction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Проверяет путь от provider JSON до Pydantic."""

    captured_payload: dict[str, Any] = {}

    extraction_data = {
        "intent": "plan_trip",
        "destination": "Япония",
        "duration_days": 7,
        "travel_period": "Осенью",
        "budget": None,
        "interests": "Природа",
    }

    async def fake_request_model(
        **arguments: Any,
    ) -> LLMProviderResponse:
        captured_payload.update(arguments["payload"])

        return build_provider_response(
            json.dumps(
                extraction_data,
                ensure_ascii=False,
            )
        )

    settings = SimpleNamespace(
        llm_base_url="https://example.com/v1",
        llm_api_key=PRIVATE_API_KEY,
        llm_model="test-model",
    )

    monkeypatch.setattr(
        ai_service,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        ai_service,
        "_request_model",
        fake_request_model,
    )
    monkeypatch.setattr(
        ai_service.httpx,
        "AsyncClient",
        DummyAsyncClient,
    )

    with caplog.at_level(
        logging.INFO,
        logger=ai_service.__name__,
    ):
        extraction = await analyze_trip_message(
            user_message=PRIVATE_MESSAGE,
            draft=TripDraft(),
        )

    assert extraction.destination == "Япония"
    assert extraction.duration_days == 7

    response_format = captured_payload["response_format"]

    assert response_format["json_schema"]["name"] == "trip_intake"
    assert response_format["json_schema"]["strict"] is True

    service_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
    )

    assert PRIVATE_MESSAGE not in service_logs
    assert PRIVATE_API_KEY not in service_logs


@pytest.mark.asyncio
async def test_analyze_trip_message_retries_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет один retry и безопасную итоговую ошибку."""

    request_count = 0

    async def fake_request_model(
        **_: object,
    ) -> LLMProviderResponse:
        nonlocal request_count
        request_count += 1

        return build_provider_response(
            '{"intent":"plan_trip"}'
        )

    settings = SimpleNamespace(
        llm_base_url="https://example.com/v1",
        llm_api_key=PRIVATE_API_KEY,
        llm_model="test-model",
    )

    monkeypatch.setattr(
        ai_service,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        ai_service,
        "_request_model",
        fake_request_model,
    )
    monkeypatch.setattr(
        ai_service.httpx,
        "AsyncClient",
        DummyAsyncClient,
    )

    with pytest.raises(
        AIServiceError,
        match="Не удалось понять сообщение",
    ):
        await analyze_trip_message(
            user_message="Хочу в Японию",
            draft=TripDraft(),
        )

    assert request_count == 2