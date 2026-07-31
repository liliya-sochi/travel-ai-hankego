"""
Бизнес-логика создания и сохранения маршрутов.
"""

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.trip import TripRepository
from app.repositories.user import UserRepository
from app.schemas.trip import TripCreateResponse
from app.services.ai import generate_trip_plan


logger = logging.getLogger(__name__)


class TripServiceError(Exception):
    """
    Безопасная ошибка сохранения маршрута.
    """


class TripService:
    """
    Управляет созданием и сохранением маршрута.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Создаёт сервис для одной SQLAlchemy-сессии.
        """

        self._session = session
        self._user_repository = UserRepository(session)
        self._trip_repository = TripRepository(session)

    async def create_trip_plan(
        self,
        telegram_id: int,
        first_name: str,
        prompt: str,
    ) -> TripCreateResponse:
        """
        Генерирует маршрут и сохраняет его пользователю.
        """

        # LLM вызывается до начала операций с PostgreSQL,
        # чтобы не держать транзакцию открытой десятки секунд.
        trip_plan = await generate_trip_plan(prompt)

        try:
            # Повторный upsert безопасен и гарантирует,
            # что пользователь существует перед INSERT маршрута.
            user = await self._user_repository.upsert_telegram_user(
                telegram_id=telegram_id,
                first_name=first_name,
            )

            trip = await self._trip_repository.create_trip(
                user_id=user.id,
                destination=trip_plan.destination,
                duration_days=trip_plan.duration_days,
                plan_data=trip_plan.model_dump(mode="json"),
            )

            await self._session.commit()

        except SQLAlchemyError as error:
            await self._session.rollback()

            # Не записываем prompt и персональные данные в лог.
            logger.exception(
                "Failed to save generated trip"
            )

            raise TripServiceError(
                "Маршрут создан, но не удалось сохранить его."
            ) from error

        return TripCreateResponse(
            **trip_plan.model_dump(),
            trip_id=trip.id,
            created_at=trip.created_at,
        )