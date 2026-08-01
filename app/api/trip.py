"""
HTTP-маршруты планирования путешествий.
"""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas.trip import (
    TripCreateResponse,
    TripHistoryRequest,
    TripHistoryResponse,
    TripPlanRequest,
)
from app.services.ai import AIServiceError
from app.services.trip import TripService, TripServiceError


router = APIRouter()

SessionDependency = Annotated[
    AsyncSession,
    Depends(get_session),
]


@router.post(
    "/trip-plan",
    response_model=TripCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать и сохранить план путешествия",
)
async def create_trip_plan(
    request: TripPlanRequest,
    session: SessionDependency,
) -> TripCreateResponse:
    """
    Генерирует маршрут и сохраняет его пользователю.
    """

    service = TripService(session)

    try:
        return await service.create_trip_plan(
            telegram_id=request.telegram_id,
            first_name=request.first_name,
            prompt=request.prompt,
        )

    except AIServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

    except TripServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.post(
    "/trip-history",
    response_model=TripHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Получить историю маршрутов",
)
async def get_trip_history(
    request: TripHistoryRequest,
    session: SessionDependency,
) -> TripHistoryResponse:
    """
    Возвращает последние маршруты Telegram-пользователя.
    """

    service = TripService(session)

    try:
        return await service.get_trip_history(
            telegram_id=request.telegram_id,
            limit=request.limit,
        )

    except TripServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error