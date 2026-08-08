"""
Тесты удаления сохранённых маршрутов.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import trip as trip_api
from app.database import get_session
from app.main import app
from app.schemas.trip import TripDeleteResponse
from app.services.trip import TripNotFoundError


class FakeTripDeleteService:
    """
    Поддельный сервис успешного удаления.
    """

    received_telegram_id: int | None = None
    received_trip_id: int | None = None

    def __init__(self, session: object) -> None:
        self._session = session

    async def delete_trip(
        self,
        telegram_id: int,
        trip_id: int,
    ) -> TripDeleteResponse:
        """
        Возвращает успешный тестовый ответ.
        """

        type(self).received_telegram_id = telegram_id
        type(self).received_trip_id = trip_id

        return TripDeleteResponse(
            trip_id=trip_id,
        )


class MissingTripDeleteService:
    """
    Поддельный сервис чужого или отсутствующего маршрута.
    """

    def __init__(self, session: object) -> None:
        self._session = session

    async def delete_trip(
        self,
        telegram_id: int,
        trip_id: int,
    ) -> TripDeleteResponse:
        """
        Возвращает одинаковую безопасную ошибку.
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
async def test_delete_trip_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет успешное удаление маршрута.
    """

    monkeypatch.setattr(
        trip_api,
        "TripService",
        FakeTripDeleteService,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-delete",
            json={
                "telegram_id": 9000000001,
                "trip_id": 7,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "trip_id": 7,
        "deleted": True,
    }

    assert FakeTripDeleteService.received_telegram_id == 9000000001
    assert FakeTripDeleteService.received_trip_id == 7


@pytest.mark.asyncio
async def test_delete_trip_hides_foreign_trip(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет одинаковый ответ для чужого и отсутствующего ID.
    """

    monkeypatch.setattr(
        trip_api,
        "TripService",
        MissingTripDeleteService,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trip-delete",
            json={
                "telegram_id": 9000000001,
                "trip_id": 999999,
            },
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Маршрут не найден."}
