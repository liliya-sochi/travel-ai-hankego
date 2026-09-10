"""Тесты внутреннего API редактирования маршрута."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import trip as trip_api
from app.database import get_session
from app.main import app
from app.schemas.trip import TripEditResponse
from app.services.trip import (
    TripEditUnavailableError,
    TripEditUnsupportedError,
    TripNotFoundError,
)


class FakeTripEditService:
    """Возвращает сохранённую обновлённую версию."""

    received_arguments: ClassVar[dict[str, object]] = {}

    def __init__(self, session: object) -> None:
        self._session = session

    async def edit_trip_plan(self, **arguments: object) -> TripEditResponse:
        type(self).received_arguments = arguments
        return TripEditResponse(
            trip_id=7,
            created_at=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
            destination="Токио",
            duration_days=1,
            summary="Обновлённый маршрут.",
            days=[
                {
                    "day": 1,
                    "title": "Спокойный Токио",
                    "morning": ["Прогулка"],
                    "afternoon": ["Музей"],
                    "evening": ["Отдых в кафе"],
                }
            ],
            practical_tips=["Проверяйте расписание."],
            editable=True,
        )


class ErrorTripEditService(FakeTripEditService):
    """Возвращает заданную безопасную ошибку."""

    error: Exception

    async def edit_trip_plan(self, **_: object) -> TripEditResponse:
        raise type(self).error


@pytest.fixture
def override_database_session() -> Iterator[None]:
    """Подменяет SQLAlchemy-сессию."""

    async def fake_get_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_session] = fake_get_session
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_trip_edit_returns_saved_full_plan(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """Проверяет успешный HTTP-контракт без внутренних данных."""

    monkeypatch.setattr(trip_api, "TripService", FakeTripEditService)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/trip-edit",
            json={
                "telegram_id": 9000000001,
                "trip_id": 7,
                "instruction": "Сделай вечер спокойнее",
            },
        )

    assert response.status_code == 200
    assert response.json()["updated"] is True
    assert response.json()["days"][0]["evening"] == ["Отдых в кафе"]
    assert "preferences" not in response.json()
    assert FakeTripEditService.received_arguments["trip_id"] == 7
    assert FakeTripEditService.received_arguments["instruction"] == (
        "Сделай вечер спокойнее"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (TripNotFoundError("Маршрут не найден."), 404),
        (
            TripEditUnavailableError("Старый маршрут нельзя редактировать."),
            409,
        ),
        (
            TripEditUnsupportedError("Создайте новую поездку."),
            422,
        ),
    ],
)
async def test_trip_edit_maps_safe_domain_errors(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
    error: Exception,
    expected_status: int,
) -> None:
    """Не раскрывает наличие чужого маршрута и объясняет ограничения."""

    ErrorTripEditService.error = error
    monkeypatch.setattr(trip_api, "TripService", ErrorTripEditService)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/trip-edit",
            json={
                "telegram_id": 9000000001,
                "trip_id": 7,
                "instruction": "Измени маршрут",
            },
        )

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(error)}
