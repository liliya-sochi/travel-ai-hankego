"""
Строгие схемы grounded-генерации маршрута.

LLM связывает конкретные места с идентификаторами Geoapify.
После детерминированной проверки результат преобразуется
в существующий публичный TripPlanResponse.
"""

from typing import Self

from pydantic import Field, model_validator

from app.schemas.geoapify import PlaceCandidate
from app.schemas.trip import (
    DayPlan,
    StrictSchema,
    TripPlanResponse,
)
from app.services.opening_hours import format_opening_hours


class GroundedActivity(StrictSchema):
    """
    Одна активность с проверяемой ссылкой на место.

    Для общей активности оба поля места должны быть null.
    Для конкретного места оба поля должны быть заполнены.
    """

    source_place_id: str | None = Field(
        min_length=1,
        max_length=500,
        description=(
            "Идентификатор места из travel_context или null для общей активности."
        ),
    )
    place_name: str | None = Field(
        min_length=1,
        max_length=500,
        description=(
            "Точное название места из travel_context или null для общей активности."
        ),
    )
    description: str = Field(
        min_length=1,
        max_length=1000,
        description="Что путешественнику предлагается сделать.",
    )

    @model_validator(mode="after")
    def validate_place_reference(self) -> Self:
        """Проверяет согласованность ID и названия места."""

        has_place_id = self.source_place_id is not None
        has_place_name = self.place_name is not None

        if has_place_id != has_place_name:
            raise ValueError(
                "source_place_id и place_name должны быть "
                "заполнены вместе или одновременно равны null."
            )

        return self


class GroundedDayPlan(StrictSchema):
    """Проверяемый план одного дня."""

    day: int = Field(
        ge=1,
    )
    title: str = Field(
        min_length=1,
        max_length=255,
    )
    morning: list[GroundedActivity] = Field(
        min_length=1,
        max_length=2,
    )
    afternoon: list[GroundedActivity] = Field(
        min_length=1,
        max_length=2,
    )
    evening: list[GroundedActivity] = Field(
        min_length=1,
        max_length=2,
    )


class GroundedTripPlanResponse(StrictSchema):
    """Внутренний Structured Output grounded-генерации."""

    destination: str = Field(
        min_length=1,
        max_length=255,
    )
    duration_days: int = Field(
        ge=1,
        le=30,
    )
    summary: str = Field(
        min_length=1,
    )
    days: list[GroundedDayPlan] = Field(
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_days(self) -> Self:
        """Проверяет количество и последовательность дней."""

        if len(self.days) != self.duration_days:
            raise ValueError(
                "Количество элементов days должно соответствовать duration_days."
            )

        actual_days = [day.day for day in self.days]
        expected_days = list(range(1, self.duration_days + 1))

        if actual_days != expected_days:
            raise ValueError(
                "Номера дней должны идти последовательно от 1 до duration_days."
            )

        return self

    def to_trip_plan_response(
        self,
        *,
        practical_tips: list[str],
        places_by_id: dict[str, PlaceCandidate],
    ) -> TripPlanResponse:
        """
        Преобразует проверенный grounded-план
        в существующий публичный контракт.

        Дополнительные сведения берутся только из
        проверенных PlaceCandidate, а не из ответа LLM.
        """

        return TripPlanResponse(
            destination=self.destination,
            duration_days=self.duration_days,
            summary=self.summary,
            days=[
                DayPlan(
                    day=day.day,
                    title=day.title,
                    morning=[
                        _format_activity(
                            activity,
                            places_by_id=places_by_id,
                        )
                        for activity in day.morning
                    ],
                    afternoon=[
                        _format_activity(
                            activity,
                            places_by_id=places_by_id,
                        )
                        for activity in day.afternoon
                    ],
                    evening=[
                        _format_activity(
                            activity,
                            places_by_id=places_by_id,
                        )
                        for activity in day.evening
                    ],
                )
                for day in self.days
            ],
            practical_tips=practical_tips,
        )


def _format_activity(
    activity: GroundedActivity,
    *,
    places_by_id: dict[str, PlaceCandidate],
) -> str:
    """Добавляет к активности только проверенные сведения о месте."""

    if activity.source_place_id is None:
        return activity.description

    place = places_by_id.get(activity.source_place_id)

    if place is None:
        raise ValueError("Grounded activity refers to an unknown place ID.")

    formatted_description = activity.description

    if not formatted_description.endswith((".", "!", "?")):
        formatted_description = f"{formatted_description}."

    activity_parts = [f"{place.name}: {formatted_description}"]

    if place.opening_hours is not None:
        formatted_opening_hours = format_opening_hours(place.opening_hours)
        source_name = (
            "Google Maps" if place.opening_hours_source == "google" else "Geoapify"
        )
        activity_parts.append(
            f"Часы по данным {source_name}: {formatted_opening_hours}."
        )

    if place.website is not None:
        activity_parts.append(f"Сайт из данных Geoapify: {place.website}")

    return " ".join(activity_parts)
