"""
HTTP-маршруты планирования путешествий.

Router принимает HTTP-запрос, вызывает сервис
и возвращает результат клиенту.
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.trip import TripPlanRequest, TripPlanResponse
from app.services.ai import AIServiceError, generate_trip_plan


router = APIRouter()


@router.post(
    "/trip-plan",
    response_model=TripPlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Создать план путешествия",
)
async def create_trip_plan(
    request: TripPlanRequest,
) -> TripPlanResponse:
    """
    Создаёт план путешествия по текстовому запросу пользователя.

    FastAPI автоматически преобразует JSON из тела запроса
    в объект TripPlanRequest.
    """

    try:
        return await generate_trip_plan(request.prompt)

    except AIServiceError as error:
        # Пользователю API не нужно знать внутренние детали Python.
        # Мы возвращаем понятную HTTP-ошибку.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(error),
        ) from error