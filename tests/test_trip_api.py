"""
Тесты API создания и чтения маршрутов.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import trip as trip_api
from app.database import get_session
from app.main import app
from app.schemas.trip import (
    TripCreateResponse,
    TripHistoryResponse,
)


class FakeTripService:
    """
    Поддельный сервис без LLM и PostgreSQL.
    """

    received_telegram_id: int | None = None
    received_first_name: str | None = None
    received_prompt: str | None = None
    received_limit: int | None = None

    def __init__(self, session: object) -> None:
        """
        Принимает тестовую сессию.
        """

        self._session = session

    async def create_trip_plan(
        self,
        telegram_id: int,
        first_name: str,
        prompt: str,
    ) -> TripCreateResponse:
        """
        Возвращает готовый тестовый маршрут.
        """

        type(self).received_telegram_id = telegram_id
        type(self).received_first_name = first_name
        type(self).received_prompt = prompt

        return TripCreateResponse(
            trip_id=7,
            created_at=datetime(
                2026,
                7,
                31,
                8,
                0,
                tzinfo=UTC,
            ),
            destination="Стамбул",
            duration_days=1,
            summary="Тестовый маршрут.",
            days=[
                {
                    "day": 1,
                    "title": "Исторический центр",
                    "morning": ["Прогулка"],
                    "afternoon": ["Музей"],
                    "evening": ["Ужин"],
                }
            ],
            practical_tips=[
                "Проверяйте актуальное расписание."
            ],
        )

    async def get_trip_history(
        self,
        telegram_id: int,
        limit: int,
    ) -> TripHistoryResponse:
        """
        Возвращает тестовую историю маршрутов.
        """

        type(self).received_telegram_id = telegram_id
        type(self).received_limit = limit

        return TripHistoryResponse(
            count=2,
            trips=[
                {
                    "trip_id": 8,
                    "destination": "Токио",
                    "duration_days": 7,
                    "created_at": datetime(
                        2026,
                        8,
                        1,
                        8,
                        0,
                        tzinfo=UTC,
                    ),
                },
                {
                    "trip_id": 7,
                    "destination": "Стамбул",
                    "duration_days": 3,
                    "created_at": datetime(
                        2026,
                        7,
                        31,
                        8,
                        0,
                        tzinfo=UTC,
                    ),
                },
            ],
        )


@pytest.fixture
def override_database_session() -> Iterator[None]:
    """
    Заменяет настоящую SQLAlchemy-сессию.
    """

    async def fake_get_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_session] = fake_get_session

    yield

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_trip_returns_persisted_trip(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет контракт успешного создания маршрута.
    """

    monkeypatch.setattr(
        trip_api,
        "TripService",
        FakeTripService,
    )

    transport = ASGITransport(app=app)

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
                    "Хочу на один день в Стамбул."
                ),
            },
        )

    response_data = response.json()

    assert response.status_code == 201
    assert response_data["trip_id"] == 7
    assert response_data["destination"] == "Стамбул"
    assert "created_at" in response_data

    assert "telegram_id" not in response_data
    assert "first_name" not in response_data
    assert "prompt" not in response_data

    assert FakeTripService.received_telegram_id == 9000000001
    assert FakeTripService.received_first_name == "Liliya"
    assert (
        FakeTripService.received_prompt
        == "Хочу на один день в Стамбул."
    )


@pytest.mark.asyncio
async def test_create_trip_rejects_unknown_field(
    override_database_session: None,
) -> None:
    """
    Проверяет запрет неизвестных полей.
    """

    transport = ASGITransport(app=app)

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
                    "Хочу на один день в Стамбул."
                ),
                "admin": True,
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_trip_history_returns_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет получение краткой истории маршрутов.
    """

    monkeypatch.setattr(
        trip_api,
        "TripService",
        FakeTripService,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-history",
            json={
                "telegram_id": 9000000001,
                "limit": 10,
            },
        )

    response_data = response.json()

    assert response.status_code == 200
    assert response_data["count"] == 2
    assert len(response_data["trips"]) == 2
    assert response_data["trips"][0]["destination"] == "Токио"

    assert "telegram_id" not in response_data
    assert "plan_data" not in response_data["trips"][0]

    assert FakeTripService.received_telegram_id == 9000000001
    assert FakeTripService.received_limit == 10