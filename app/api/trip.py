"""
HTTP-маршруты планирования путешествий.
"""

from math import ceil
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_trip_plan_rate_limiter,
)
from app.database import get_session
from app.schemas.trip import (
    TripCreateResponse,
    TripDeleteRequest,
    TripDeleteResponse,
    TripDetailsRequest,
    TripDetailsResponse,
    TripHistoryRequest,
    TripHistoryResponse,
    TripPlanRequest,
)
from app.services.ai import AIServiceError
from app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    TripPlanRateLimiter,
)
from app.services.trip import (
    TripNotFoundError,
    TripService,
    TripServiceError,
)


router = APIRouter()

SessionDependency = Annotated[
    AsyncSession,
    Depends(get_session),
]

RateLimiterDependency = Annotated[
    TripPlanRateLimiter,
    Depends(get_trip_plan_rate_limiter),
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
    rate_limiter: RateLimiterDependency,
) -> TripCreateResponse:
    """
    Генерирует маршрут и сохраняет его пользователю.
    """

    try:
        await rate_limiter.check(
            telegram_id=request.telegram_id,
        )

    except RateLimitExceededError as error:
        retry_minutes = max(
            1,
            ceil(error.retry_after_seconds / 60),
        )

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Лимит генерации маршрутов исчерпан. "
                f"Попробуйте снова примерно через "
                f"{retry_minutes} мин."
            ),
            headers={
                "Retry-After": str(
                    error.retry_after_seconds
                ),
            },
        ) from error

    except RateLimitUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Генерация маршрутов временно недоступна."
            ),
        ) from error

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


@router.post(
    "/trip-details",
    response_model=TripDetailsResponse,
    status_code=status.HTTP_200_OK,
    summary="Получить сохранённый маршрут",
)
async def get_trip_details(
    request: TripDetailsRequest,
    session: SessionDependency,
) -> TripDetailsResponse:
    """
    Возвращает полный маршрут только его владельцу.
    """

    service = TripService(session)

    try:
        return await service.get_trip_details(
            telegram_id=request.telegram_id,
            trip_id=request.trip_id,
        )

    except TripNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except TripServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error


@router.post(
    "/trip-delete",
    response_model=TripDeleteResponse,
    status_code=status.HTTP_200_OK,
    summary="Удалить сохранённый маршрут",
)
async def delete_trip(
    request: TripDeleteRequest,
    session: SessionDependency,
) -> TripDeleteResponse:
    """
    Удаляет маршрут только его владельца.
    """

    service = TripService(session)

    try:
        return await service.delete_trip(
            telegram_id=request.telegram_id,
            trip_id=request.trip_id,
        )

    except TripNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except TripServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error