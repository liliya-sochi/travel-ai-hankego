"""
Pydantic-схемы системных endpoint HankeGo.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


ComponentStatus = Literal[
    "up",
    "down",
]

ReadinessStatus = Literal[
    "ready",
    "not_ready",
]


class StrictSystemSchema(BaseModel):
    """
    Базовая строгая схема системных ответов.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )


class InfoResponse(StrictSystemSchema):
    """
    Информация о запущенном приложении.
    """

    name: str
    version: str


class LivenessResponse(StrictSystemSchema):
    """
    Результат проверки Python-процесса.
    """

    status: Literal["alive"]


class DependencyChecks(StrictSystemSchema):
    """
    Состояние обязательной инфраструктуры.
    """

    postgresql: ComponentStatus
    redis: ComponentStatus


class ReadinessResponse(StrictSystemSchema):
    """
    Результат проверки готовности приложения.
    """

    status: ReadinessStatus
    checks: DependencyChecks