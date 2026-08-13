"""
Тесты HTTP-контракта conversational trip intake.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import trip as trip_api
from app.api.dependencies import get_trip_intake_rate_limiter
from app.main import app
from app.schemas.trip import TripIntakeResponse
from app.services.ai import AIServiceError
from app.services.rate_limit import RateLimitExceededError


class BlockingTripIntakeRateLimiter:
    """Всегда блокирует новый LLM-разбор сообщения."""

    async def check(
        self,
        telegram_id: int,
    ) -> None:
        """Имитирует превышение отдельного intake-лимита."""

        raise RateLimitExceededError(
            retry_after_seconds=125,
        )


@pytest.mark.asyncio
async def test_trip_intake_returns_ready_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет успешный HTTP-контракт intake."""

    async def fake_process_trip_message(
        *,
        user_message: str,
        draft: object,
    ) -> TripIntakeResponse:
        assert user_message == "На неделю осенью"
        assert draft.destination == "Япония"

        return TripIntakeResponse.model_validate(
            {
                "intent": "plan_trip",
                "draft": {
                    "destination": "Япония",
                    "duration_days": 7,
                    "travel_period": "Осенью",
                    "budget": None,
                    "interests": None,
                },
                "missing_required_fields": [],
                "ready_to_generate": True,
                "next_question": None,
            }
        )

    monkeypatch.setattr(
        trip_api,
        "process_trip_message",
        fake_process_trip_message,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-intake",
            json={
                "telegram_id": 9000000001,
                "user_message": "На неделю осенью",
                "draft": {
                    "destination": "Япония",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["ready_to_generate"] is True
    assert response.json()["draft"]["duration_days"] == 7


@pytest.mark.asyncio
async def test_trip_intake_converts_ai_error_to_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет безопасную HTTP-ошибку LLM."""

    async def fake_process_trip_message(
        **_: object,
    ) -> TripIntakeResponse:
        raise AIServiceError("Не удалось понять сообщение.")

    monkeypatch.setattr(
        trip_api,
        "process_trip_message",
        fake_process_trip_message,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-intake",
            json={
                "telegram_id": 9000000001,
                "user_message": "Хочу куда-нибудь",
                "draft": {},
            },
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Не удалось понять сообщение."}


@pytest.mark.asyncio
async def test_trip_intake_returns_separate_429() -> None:
    """Проверяет отдельный лимит уточняющих сообщений."""

    app.dependency_overrides[get_trip_intake_rate_limiter] = lambda: (
        BlockingTripIntakeRateLimiter()
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-intake",
            json={
                "telegram_id": 9000000001,
                "user_message": "Хочу в Японию",
                "draft": {},
            },
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "125"
    assert "Слишком много сообщений" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trip_intake_rejects_long_message() -> None:
    """Проверяет валидацию до обращения к LLM."""

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-intake",
            json={
                "telegram_id": 9000000001,
                "user_message": "a" * 2001,
                "draft": {},
            },
        )

    assert response.status_code == 422
