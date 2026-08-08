"""
Тесты HTTP API регистрации пользователей.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import user as user_api
from app.database import get_session
from app.main import app


class FakeUserService:
    """
    Поддельный сервис пользователя для изолированного теста API.

    Он не обращается к PostgreSQL.
    """

    received_telegram_id: int | None = None
    received_first_name: str | None = None

    def __init__(self, session: object) -> None:
        """
        Принимает тестовую сессию, как настоящий UserService.
        """

        self._session = session

    async def register_telegram_user(
        self,
        telegram_id: int,
        first_name: str,
    ) -> SimpleNamespace:
        """
        Запоминает полученные данные и возвращает тестового пользователя.
        """

        type(self).received_telegram_id = telegram_id
        type(self).received_first_name = first_name

        return SimpleNamespace(
            id=1,
            first_name=first_name,
            created_at=datetime(
                2026,
                7,
                30,
                8,
                0,
                tzinfo=UTC,
            ),
        )


@pytest.fixture
def override_database_session() -> Iterator[None]:
    """
    Заменяет настоящую SQLAlchemy-сессию тестовым объектом.
    """

    async def fake_get_session() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_session] = fake_get_session

    yield

    # После теста возвращаем приложение в исходное состояние.
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_upsert_telegram_user_returns_safe_response(
    monkeypatch: pytest.MonkeyPatch,
    override_database_session: None,
) -> None:
    """
    Проверяет успешную регистрацию и безопасный HTTP-ответ.
    """

    monkeypatch.setattr(
        user_api,
        "UserService",
        FakeUserService,
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/v1/users/telegram",
            json={
                "telegram_id": 9000000001,
                "first_name": "Liliya",
            },
        )

    response_data = response.json()

    assert response.status_code == 200
    assert response_data["id"] == 1
    assert response_data["first_name"] == "Liliya"
    assert "created_at" in response_data

    # Telegram ID не должен возвращаться клиенту.
    assert "telegram_id" not in response_data

    # Проверяем, что endpoint передал сервису правильные данные.
    assert FakeUserService.received_telegram_id == 9000000001
    assert FakeUserService.received_first_name == "Liliya"


@pytest.mark.asyncio
async def test_upsert_telegram_user_rejects_unknown_field(
    override_database_session: None,
) -> None:
    """
    Проверяет запрет неизвестных полей в JSON.
    """

    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/v1/users/telegram",
            json={
                "telegram_id": 9000000001,
                "first_name": "Liliya",
                "secret": "unexpected",
            },
        )

    assert response.status_code == 422

    validation_errors = response.json()["detail"]

    assert any(error["type"] == "extra_forbidden" for error in validation_errors)
