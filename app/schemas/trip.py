"""
Pydantic-схемы для планирования путешествия.

Схемы определяют строгий формат:
- входящего запроса от пользователя;
- ответа, который должен вернуть backend.
"""

from pydantic import BaseModel, Field


class TripPlanRequest(BaseModel):
    """
    Запрос пользователя на создание плана путешествия.
    """

    prompt: str = Field(
        # Минимальная длина защищает endpoint
        # от пустых или бессмысленно коротких запросов.
        min_length=10,

        # Ограничение не позволяет отправить модели
        # чрезмерно большой текст.
        max_length=2000,

        # Этот пример будет показан в Swagger.
        examples=[
            (
                "Хочу на 5 дней в Стамбул. "
                "Люблю архитектуру и местную еду."
            )
        ],
    )


class DayPlan(BaseModel):
    """
    План одного дня путешествия.
    """

    day: int = Field(
        ge=1,
        description="Номер дня, начиная с единицы.",
    )

    title: str = Field(
        min_length=1,
        description="Краткое название дня.",
    )

    activities: list[str] = Field(
        min_length=1,
        description="Список запланированных занятий и мест.",
    )


class TripPlanResponse(BaseModel):
    """
    Полный структурированный план путешествия.
    """

    destination: str = Field(
        min_length=1,
        description="Город или страна назначения.",
    )

    duration_days: int = Field(
        ge=1,
        le=30,
        description="Продолжительность поездки в днях.",
    )

    summary: str = Field(
        min_length=1,
        description="Краткое описание путешествия.",
    )

    days: list[DayPlan] = Field(
        min_length=1,
        description="Подробный план по дням.",
    )

    practical_tips: list[str] = Field(
        default_factory=list,
        description="Практические советы путешественнику.",
    )