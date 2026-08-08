"""
Integration-тесты PostgreSQL-репозиториев.

Эти тесты выполняют реальные SQL-запросы
и требуют TEST_DATABASE_URL.
"""

from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trip import Trip
from app.models.user import User
from app.repositories.trip import TripRepository
from app.repositories.user import UserRepository

pytestmark = pytest.mark.integration


def build_plan_data(
    destination: str,
) -> dict[str, Any]:
    """
    Создаёт валидный тестовый Structured Output.
    """

    return {
        "destination": destination,
        "duration_days": 1,
        "summary": "Тестовый маршрут.",
        "days": [
            {
                "day": 1,
                "title": "Первый день",
                "morning": ["Прогулка"],
                "afternoon": ["Музей"],
                "evening": ["Ужин"],
            }
        ],
        "practical_tips": ["Проверяйте актуальное расписание."],
    }


@pytest.mark.asyncio
async def test_user_upsert_updates_existing_row(
    database_session: AsyncSession,
) -> None:
    """
    Проверяет настоящий PostgreSQL upsert пользователя.
    """

    repository = UserRepository(database_session)

    created_user = await repository.upsert_telegram_user(
        telegram_id=9000000001,
        first_name="Liliya",
    )

    await database_session.commit()

    created_user_id = created_user.id

    updated_user = await repository.upsert_telegram_user(
        telegram_id=9000000001,
        first_name="Lily",
    )

    await database_session.commit()

    # Загружаем запись заново непосредственно из PostgreSQL.
    database_session.expire_all()

    stored_user = await database_session.scalar(
        select(User).where(User.telegram_id == 9000000001)
    )

    user_count = await database_session.scalar(select(func.count()).select_from(User))

    assert updated_user.id == created_user_id
    assert stored_user is not None
    assert stored_user.first_name == "Lily"
    assert user_count == 1


@pytest.mark.asyncio
async def test_trip_repository_enforces_ownership(
    database_session: AsyncSession,
) -> None:
    """
    Проверяет создание, историю и защиту владельца.
    """

    user_repository = UserRepository(database_session)
    trip_repository = TripRepository(database_session)

    owner = await user_repository.upsert_telegram_user(
        telegram_id=9000000001,
        first_name="Liliya",
    )

    other_user = await user_repository.upsert_telegram_user(
        telegram_id=9000000002,
        first_name="Other",
    )

    plan_data = build_plan_data(destination="Токио")

    created_trip = await trip_repository.create_trip(
        user_id=owner.id,
        destination="Токио",
        duration_days=1,
        plan_data=plan_data,
    )

    await database_session.commit()

    history = await trip_repository.list_by_user_id(
        user_id=owner.id,
        limit=10,
    )

    owned_trip = await trip_repository.get_by_id_and_user_id(
        trip_id=created_trip.id,
        user_id=owner.id,
    )

    foreign_trip = await trip_repository.get_by_id_and_user_id(
        trip_id=created_trip.id,
        user_id=other_user.id,
    )

    assert len(history) == 1
    assert history[0].destination == "Токио"

    assert owned_trip is not None
    assert owned_trip.plan_data == plan_data

    # Другой пользователь не видит маршрут владельца.
    assert foreign_trip is None


@pytest.mark.asyncio
async def test_user_deletion_cascades_to_trips(
    database_session: AsyncSession,
) -> None:
    """
    Проверяет ON DELETE CASCADE из Alembic-миграции.
    """

    user_repository = UserRepository(database_session)
    trip_repository = TripRepository(database_session)

    user = await user_repository.upsert_telegram_user(
        telegram_id=9000000001,
        first_name="Liliya",
    )

    await trip_repository.create_trip(
        user_id=user.id,
        destination="Стамбул",
        duration_days=1,
        plan_data=build_plan_data(destination="Стамбул"),
    )

    await database_session.commit()

    await database_session.execute(delete(User).where(User.id == user.id))

    await database_session.commit()

    trip_count = await database_session.scalar(select(func.count()).select_from(Trip))

    assert trip_count == 0


@pytest.mark.asyncio
async def test_database_rejects_invalid_trip_duration(
    database_session: AsyncSession,
) -> None:
    """
    Проверяет CHECK duration_days BETWEEN 1 AND 30.
    """

    user_repository = UserRepository(database_session)
    trip_repository = TripRepository(database_session)

    user = await user_repository.upsert_telegram_user(
        telegram_id=9000000001,
        first_name="Liliya",
    )

    await database_session.commit()

    with pytest.raises(IntegrityError):
        await trip_repository.create_trip(
            user_id=user.id,
            destination="Ошибка",
            duration_days=0,
            plan_data={
                "destination": "Ошибка",
                "duration_days": 0,
            },
        )

    await database_session.rollback()

    trip_count = await database_session.scalar(select(func.count()).select_from(Trip))

    assert trip_count == 0


@pytest.mark.asyncio
async def test_trip_repository_deletes_only_owned_trip(
    database_session: AsyncSession,
) -> None:
    """
    Проверяет атомарное удаление только владельцем.
    """

    user_repository = UserRepository(database_session)
    trip_repository = TripRepository(database_session)

    owner = await user_repository.upsert_telegram_user(
        telegram_id=9000000001,
        first_name="Liliya",
    )

    other_user = await user_repository.upsert_telegram_user(
        telegram_id=9000000002,
        first_name="Other",
    )

    trip = await trip_repository.create_trip(
        user_id=owner.id,
        destination="Токио",
        duration_days=1,
        plan_data=build_plan_data(destination="Токио"),
    )

    await database_session.commit()

    foreign_delete_result = await trip_repository.delete_by_id_and_user_id(
        trip_id=trip.id,
        user_id=other_user.id,
    )

    assert foreign_delete_result is None

    existing_trip = await trip_repository.get_by_id_and_user_id(
        trip_id=trip.id,
        user_id=owner.id,
    )

    assert existing_trip is not None

    owner_delete_result = await trip_repository.delete_by_id_and_user_id(
        trip_id=trip.id,
        user_id=owner.id,
    )

    await database_session.commit()

    deleted_trip = await trip_repository.get_by_id_and_user_id(
        trip_id=trip.id,
        user_id=owner.id,
    )

    assert owner_delete_result == trip.id
    assert deleted_trip is None
