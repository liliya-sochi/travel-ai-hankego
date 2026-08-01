"""
Pydantic-схемы планирования путешествий.
"""

from datetime import datetime
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class StrictSchema(BaseModel):
    """
    Базовая строгая схема HankeGo.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


class TripPlanRequest(StrictSchema):
    """
    Запрос на создание и сохранение маршрута.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    first_name: str = Field(
        min_length=1,
        max_length=255,
        description="Имя пользователя Telegram.",
        examples=["Liliya"],
    )

    prompt: str = Field(
        min_length=10,
        max_length=2000,
        description="Описание желаемой поездки.",
        examples=[
            (
                "Хочу на 5 дней в Стамбул. "
                "Люблю архитектуру и местную еду."
            )
        ],
    )


class TripHistoryRequest(StrictSchema):
    """
    Запрос истории маршрутов пользователя.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=20,
        description="Максимальное количество маршрутов.",
    )


class TripDetailsRequest(StrictSchema):
    """
    Запрос полного сохранённого маршрута.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
        examples=[7],
    )


class DayPlan(StrictSchema):
    """
    План одного дня путешествия.
    """

    day: int = Field(
        ge=1,
        description="Номер дня, начиная с единицы.",
    )

    title: str = Field(
        min_length=1,
        max_length=255,
        description="Краткая тема дня.",
    )

    morning: list[str] = Field(
        default_factory=list,
        description="План на утро.",
    )

    afternoon: list[str] = Field(
        default_factory=list,
        description="План на день.",
    )

    evening: list[str] = Field(
        default_factory=list,
        description="План на вечер.",
    )


class TripPlanResponse(StrictSchema):
    """
    Проверенный Structured Output языковой модели.
    """

    destination: str = Field(
        min_length=1,
        max_length=255,
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
        description="Практические советы.",
    )

    @model_validator(mode="after")
    def validate_days(self) -> Self:
        """
        Проверяет количество и последовательность дней.
        """

        if len(self.days) != self.duration_days:
            raise ValueError(
                "Количество элементов days должно "
                "соответствовать duration_days."
            )

        actual_days = [
            day.day
            for day in self.days
        ]

        expected_days = list(
            range(1, self.duration_days + 1)
        )

        if actual_days != expected_days:
            raise ValueError(
                "Номера дней должны идти последовательно "
                "от 1 до duration_days."
            )

        return self


class TripCreateResponse(TripPlanResponse):
    """
    Сохранённый маршрут, возвращаемый клиенту.
    """

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    created_at: datetime = Field(
        description="Время сохранения маршрута.",
    )


class TripDetailsResponse(TripPlanResponse):
    """
    Полный сохранённый маршрут пользователя.
    """

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    created_at: datetime = Field(
        description="Время сохранения маршрута.",
    )


class TripSummaryResponse(StrictSchema):
    """
    Краткая информация о сохранённом маршруте.
    """

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    destination: str = Field(
        min_length=1,
        max_length=255,
        description="Направление поездки.",
    )

    duration_days: int = Field(
        ge=1,
        le=30,
        description="Продолжительность поездки.",
    )

    created_at: datetime = Field(
        description="Время сохранения маршрута.",
    )


class TripHistoryResponse(StrictSchema):
    """
    История маршрутов пользователя.
    """

    count: int = Field(
        ge=0,
        description="Количество возвращённых маршрутов.",
    )

    trips: list[TripSummaryResponse] = Field(
        default_factory=list,
        description="Последние сохранённые маршруты.",
    )