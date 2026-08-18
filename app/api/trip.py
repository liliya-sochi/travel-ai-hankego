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
    get_trip_enrichment_service,
    get_trip_generation_lock,
    get_trip_intake_rate_limiter,
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
    TripIntakeRequest,
    TripIntakeResponse,
    TripPlanRequest,
)
from app.services.ai import AIServiceError
from app.services.rate_limit import (
    RateLimitExceededError,
    RateLimitUnavailableError,
    TripIntakeRateLimiter,
    TripPlanRateLimiter,
)
from app.services.trip import (
    TripNotFoundError,
    TripService,
    TripServiceError,
)
from app.services.trip_enrichment import (
    TripEnrichmentError,
    TripEnrichmentService,
)
from app.services.trip_intake import process_trip_message
from app.services.trip_lock import (
    TripGenerationInProgressError,
    TripGenerationLock,
    TripGenerationLockUnavailableError,
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

GenerationLockDependency = Annotated[
    TripGenerationLock,
    Depends(get_trip_generation_lock),
]

EnrichmentServiceDependency = Annotated[
    TripEnrichmentService,
    Depends(get_trip_enrichment_service),
]

IntakeRateLimiterDependency = Annotated[
    TripIntakeRateLimiter,
    Depends(get_trip_intake_rate_limiter),
]


@router.post(
    "/trip-intake",
    response_model=TripIntakeResponse,
    status_code=status.HTTP_200_OK,
    summary="Разобрать сообщение о поездке",
)
async def analyze_trip_intake(
    request: TripIntakeRequest,
    rate_limiter: IntakeRateLimiterDependency,
) -> TripIntakeResponse:
    """
    Извлекает параметры и определяет следующий шаг диалога.
    """

    try:
        await rate_limiter.check(
            telegram_id=request.telegram_id,
        )

        return await process_trip_message(
            user_message=request.user_message,
            draft=request.draft,
        )

    except RateLimitExceededError as error:
        retry_minutes = max(
            1,
            ceil(error.retry_after_seconds / 60),
        )

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Слишком много сообщений для планирования. "
                f"Попробуйте снова примерно через "
                f"{retry_minutes} мин."
            ),
            headers={
                "Retry-After": str(error.retry_after_seconds),
            },
        ) from error

    except RateLimitUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Планирование поездки временно недоступно.",
        ) from error

    except AIServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error


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
    generation_lock: GenerationLockDependency,
    enrichment_service: EnrichmentServiceDependency,
) -> TripCreateResponse:
    """
    Генерирует маршрут и сохраняет его пользователю.
    """

    service = TripService(session)

    try:
        async with generation_lock.hold(
            telegram_id=request.telegram_id,
        ):
            await rate_limiter.check(
                telegram_id=request.telegram_id,
            )

            return await service.create_trip_plan(
                telegram_id=request.telegram_id,
                first_name=request.first_name,
                preferences=request.preferences,
                enrichment_service=enrichment_service,
            )

    except TripGenerationInProgressError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Ваш маршрут уже создаётся. Дождитесь завершения текущей генерации."
            ),
        ) from error

    except TripGenerationLockUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Генерация маршрутов временно недоступна."),
        ) from error

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
                "Retry-After": str(error.retry_after_seconds),
            },
        ) from error

    except RateLimitUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Генерация маршрутов временно недоступна."),
        ) from error

    except TripEnrichmentError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error

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
