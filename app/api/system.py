"""
Системные HTTP-endpoint HankeGo.
"""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Response,
    status,
)

from app.api.dependencies import get_health_service
from app.schemas.system import (
    InfoResponse,
    LivenessResponse,
    ReadinessResponse,
)
from app.services.health import HealthService

router = APIRouter(
    tags=["System"],
)


HealthServiceDependency = Annotated[
    HealthService,
    Depends(get_health_service),
]


@router.get(
    "/info",
    response_model=InfoResponse,
    status_code=status.HTTP_200_OK,
    summary="Получить информацию о backend",
)
async def get_info() -> InfoResponse:
    """
    Возвращает название и версию приложения.
    """

    return InfoResponse(
        name="HankeGo API",
        version="0.1.0",
    )


@router.get(
    "/health/live",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
    summary="Проверить работу процесса API",
)
async def get_liveness() -> LivenessResponse:
    """
    Подтверждает, что процесс FastAPI отвечает.
    """

    return LivenessResponse(
        status="alive",
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": ("Одна или несколько зависимостей недоступны."),
        },
    },
    summary="Проверить готовность backend",
)
async def get_readiness(
    response: Response,
    health_service: HealthServiceDependency,
) -> ReadinessResponse:
    """
    Проверяет PostgreSQL и Redis.
    """

    readiness = await health_service.check_readiness()

    if readiness.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return readiness
