"""
Репозиторий маршрутов.

Содержит только операции чтения и записи PostgreSQL.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trip import Trip


class TripRepository:
    """
    Выполняет SQL-операции с таблицей trips.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Получает SQLAlchemy-сессию текущего HTTP-запроса.
        """

        self._session = session

    async def create_trip(
        self,
        user_id: int,
        destination: str,
        duration_days: int,
        plan_data: dict[str, Any],
    ) -> Trip:
        """
        Создаёт сохранённый маршрут пользователя.
        """

        trip = Trip(
            user_id=user_id,
            destination=destination,
            duration_days=duration_days,
            plan_data=plan_data,
        )

        self._session.add(trip)

        await self._session.flush()
        await self._session.refresh(trip)

        return trip

    async def list_by_user_id(
        self,
        user_id: int,
        limit: int,
    ) -> list[Trip]:
        """
        Возвращает последние маршруты пользователя.
        """

        statement = (
            select(Trip)
            .where(Trip.user_id == user_id)
            .order_by(
                Trip.created_at.desc(),
                Trip.id.desc(),
            )
            .limit(limit)
        )

        result = await self._session.execute(statement)

        return list(result.scalars().all())

    async def get_by_id_and_user_id(
        self,
        trip_id: int,
        user_id: int,
    ) -> Trip | None:
        """
        Возвращает маршрут только его владельцу.
        """

        statement = select(Trip).where(
            Trip.id == trip_id,
            Trip.user_id == user_id,
        )

        result = await self._session.execute(statement)

        return result.scalar_one_or_none()

    async def delete_by_id_and_user_id(
        self,
        trip_id: int,
        user_id: int,
    ) -> int | None:
        """
        Удаляет маршрут только его владельца.

        Возвращает ID удалённой записи или None.
        """

        statement = (
            delete(Trip)
            .where(
                Trip.id == trip_id,
                Trip.user_id == user_id,
            )
            .returning(Trip.id)
        )

        result = await self._session.execute(statement)

        return result.scalar_one_or_none()
