"""
Pydantic-модели HankeGo.

Пока здесь описываются только модели ответов API.
Позже появятся модели запросов, пользователей,
поездок и других сущностей.
"""

from pydantic import BaseModel


class ProjectInfo(BaseModel):
    """
    Информация о приложении.
    """

    project: str
    version: str
    status: str


class HealthStatus(BaseModel):
    """
    Техническое состояние приложения.
    """

    status: str
    environment: str