"""
Маршруты с общей информацией о приложении HankeGo.

Здесь находятся HTTP-маршруты и модели данных,
которые используются только этими маршрутами.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings


class ProjectInfo(BaseModel):
    """
    Описывает структуру ответа информационного маршрута.

    Каждый ответ GET /api/v1/info должен содержать
    именно эти поля и соответствовать указанным типам.
    """

    project: str
    version: str
    environment: str
    description: str


# Создаём отдельную группу информационных маршрутов.
# Основное приложение подключает её в app/main.py.
router = APIRouter()


@router.get("/info", response_model=ProjectInfo)
async def get_info() -> ProjectInfo:
    """
    Возвращает общую информацию о HankeGo.
    """

    return ProjectInfo(
        project=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        description="AI-powered travel platform",
    )