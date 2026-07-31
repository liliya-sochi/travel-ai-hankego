"""
Репозиторий маршрутов.

Содержит только операции записи в PostgreSQL.
"""

from typing import Any

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

        # Отправляем INSERT в PostgreSQL,
        # не завершая транзакцию.
        await self._session.flush()

        # Получаем id и created_at, созданные PostgreSQL.
        await self._session.refresh(trip)

        return trip