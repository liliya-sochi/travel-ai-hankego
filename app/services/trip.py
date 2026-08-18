"""
Бизнес-логика создания и сохранения маршрутов.
"""

import logging

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.trip import TripRepository
from app.repositories.user import UserRepository
from app.schemas.trip import (
    TripCreateResponse,
    TripDeleteResponse,
    TripDetailsResponse,
    TripHistoryResponse,
    TripPreferences,
    TripSummaryResponse,
)
from app.services.ai import generate_trip_plan
from app.services.trip_enrichment import TripEnrichmentService

logger = logging.getLogger(__name__)


class TripServiceError(Exception):
    """
    Безопасная ошибка работы с маршрутами.
    """


class TripNotFoundError(Exception):
    """
    Маршрут не существует или не принадлежит пользователю.
    """


class TripService:
    """
    Управляет созданием и чтением маршрутов.
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
        preferences: TripPreferences,
        enrichment_service: TripEnrichmentService,
    ) -> TripCreateResponse:
        """
        Обогащает параметры, генерирует и сохраняет маршрут.
        """

        # Внешние API вызываются до операций с PostgreSQL,
        # чтобы не держать транзакцию открытой во время сети.
        travel_context = await enrichment_service.enrich(preferences)

        trip_plan = await generate_trip_plan(
            preferences=preferences,
            travel_context=travel_context,
        )

        try:
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

            # Не записываем параметры поездки
            # и персональные данные в лог.
            logger.exception("Failed to save generated trip")

            raise TripServiceError(
                "Маршрут создан, но не удалось сохранить его."
            ) from error

        return TripCreateResponse(
            **trip_plan.model_dump(),
            trip_id=trip.id,
            created_at=trip.created_at,
        )

    async def get_trip_history(
        self,
        telegram_id: int,
        limit: int,
    ) -> TripHistoryResponse:
        """
        Возвращает последние маршруты Telegram-пользователя.
        """

        try:
            user = await self._user_repository.get_by_telegram_id(
                telegram_id=telegram_id,
            )

            if user is None:
                return TripHistoryResponse(
                    count=0,
                    trips=[],
                )

            trips = await self._trip_repository.list_by_user_id(
                user_id=user.id,
                limit=limit,
            )

        except SQLAlchemyError as error:
            await self._session.rollback()

            # Telegram ID не записываем в лог.
            logger.exception("Failed to load trip history")

            raise TripServiceError(
                "Не удалось загрузить сохранённые маршруты."
            ) from error

        trip_items = [
            TripSummaryResponse(
                trip_id=trip.id,
                destination=trip.destination,
                duration_days=trip.duration_days,
                created_at=trip.created_at,
            )
            for trip in trips
        ]

        return TripHistoryResponse(
            count=len(trip_items),
            trips=trip_items,
        )

    async def get_trip_details(
        self,
        telegram_id: int,
        trip_id: int,
    ) -> TripDetailsResponse:
        """
        Возвращает полный маршрут только его владельцу.
        """

        try:
            user = await self._user_repository.get_by_telegram_id(
                telegram_id=telegram_id,
            )

            if user is None:
                raise TripNotFoundError("Маршрут не найден.")

            trip = await self._trip_repository.get_by_id_and_user_id(
                trip_id=trip_id,
                user_id=user.id,
            )

            if trip is None:
                raise TripNotFoundError("Маршрут не найден.")

            return TripDetailsResponse(
                **trip.plan_data,
                trip_id=trip.id,
                created_at=trip.created_at,
            )

        except TripNotFoundError:
            raise

        except ValidationError as error:
            logger.exception("Invalid persisted trip data")

            raise TripServiceError(
                "Не удалось прочитать сохранённый маршрут."
            ) from error

        except SQLAlchemyError as error:
            await self._session.rollback()

            # Не записываем Telegram ID в лог.
            logger.exception("Failed to load trip details")

            raise TripServiceError(
                "Не удалось загрузить сохранённый маршрут."
            ) from error

    async def delete_trip(
        self,
        telegram_id: int,
        trip_id: int,
    ) -> TripDeleteResponse:
        """
        Удаляет маршрут только его владельца.
        """

        try:
            user = await self._user_repository.get_by_telegram_id(
                telegram_id=telegram_id,
            )

            if user is None:
                raise TripNotFoundError("Маршрут не найден.")

            deleted_trip_id = await self._trip_repository.delete_by_id_and_user_id(
                trip_id=trip_id,
                user_id=user.id,
            )

            if deleted_trip_id is None:
                raise TripNotFoundError("Маршрут не найден.")

            await self._session.commit()

        except TripNotFoundError:
            await self._session.rollback()
            raise

        except SQLAlchemyError as error:
            await self._session.rollback()

            # Не записываем пользовательские ID в лог.
            logger.exception("Failed to delete trip")

            raise TripServiceError("Не удалось удалить сохранённый маршрут.") from error

        return TripDeleteResponse(
            trip_id=deleted_trip_id,
        )
