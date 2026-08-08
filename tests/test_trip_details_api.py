"""
Тесты получения полного сохранённого маршрута.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import trip as trip_api
from app.database import get_session
from app.main import app
from app.schemas.trip import TripDetailsResponse
from app.services.trip import TripNotFoundError


class FakeTripDetailsService:
    """
    Поддельный сервис успешного получения маршрута.
    """

    received_telegram_id: int | None = None
    received_trip_id: int | None = None

    def __init__(self, session: object) -> None:
        self._session = session

    async def get_trip_details(
        self,
        telegram_id: int,
        trip_id: int,
    ) -> TripDetailsResponse:
        """
        Возвращает тестовый сохранённый маршрут.
        """

        type(self).received_telegram_id = telegram_id
        type(self).received_trip_id = trip_id

        return TripDetailsResponse(
            trip_id=trip_id,
            created_at=datetime(
                2026,
                8,
                1,
                8,
                0,
                tzinfo=UTC,
            ),
            destination="Токио",
            duration_days=1,
            summary="Тестовый сохранённый маршрут.",
            days=[
                {
                    "day": 1,
                    "title": "Центр Токио",
                    "morning": ["Прогулка"],
                    "afternoon": ["Музей"],
                    "evening": ["Ужин"],
                }
            ],
            practical_tips=["Проверяйте расписание."],
        )


class MissingTripService:
    """
    Поддельный сервис отсутствующего маршрута.
    """

    def __init__(self, session: object) -> None:
        self._session = session

    async def get_trip_details(
        self,
        telegram_id: int,
        trip_id: int,
    ) -> TripDetailsResponse:
        """
        Имитирует отсутствующий или чужой маршрут.
        """

        raise TripNotFoundError("Маршрут не найден.")


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
async def test_trip_details_returns_owned_trip(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет успешный ответ полного маршрута.
    """

    monkeypatch.setattr(
        trip_api,
        "TripService",
        FakeTripDetailsService,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-details",
            json={
                "telegram_id": 9000000001,
                "trip_id": 7,
            },
        )

    response_data = response.json()

    assert response.status_code == 200
    assert response_data["trip_id"] == 7
    assert response_data["destination"] == "Токио"
    assert response_data["days"][0]["day"] == 1

    assert "telegram_id" not in response_data
    assert "user_id" not in response_data

    assert FakeTripDetailsService.received_telegram_id == 9000000001
    assert FakeTripDetailsService.received_trip_id == 7


@pytest.mark.asyncio
async def test_trip_details_hides_missing_or_foreign_trip(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет одинаковый ответ для чужого и отсутствующего ID.
    """

    monkeypatch.setattr(
        trip_api,
        "TripService",
        MissingTripService,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-details",
            json={
                "telegram_id": 9000000001,
                "trip_id": 999999,
            },
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Маршрут не найден."}
