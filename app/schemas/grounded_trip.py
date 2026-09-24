"""
Строгие схемы grounded-генерации маршрута.

LLM связывает конкретные места с идентификаторами Geoapify.
После детерминированной проверки результат преобразуется
в существующий публичный TripPlanResponse.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.geoapify import PlaceCandidate
from app.schemas.trip import (
    DayPlan,
    StrictSchema,
    TripPlanResponse,
)
from app.services.opening_hours import format_opening_hours

GroundedActivityFocus = Literal[
    "architecture",
    "museum",
    "food",
    "park",
    "entertainment",
    "sight",
    "place",
]


ACTIVITY_FOCUS_CATEGORY_PREFIXES: dict[
    GroundedActivityFocus,
    str | None,
] = {
    "architecture": "building.tourism",
    "museum": "entertainment.museum",
    "food": "catering.restaurant",
    "park": "leisure.park",
    "entertainment": "entertainment",
    "sight": "tourism.sights",
    "place": None,
}


ACTIVITY_FOCUS_DESCRIPTIONS: dict[
    GroundedActivityFocus,
    str,
] = {
    "architecture": "осмотреть архитектурный объект",
    "museum": "посетить музей",
    "food": "познакомиться с местной кухней",
    "park": "прогуляться по парку",
    "entertainment": "посетить развлекательное место",
    "sight": "осмотреть достопримечательность",
    "place": "посетить место",
}


INTEREST_CATEGORY_ACTIVITY_FOCUSES: dict[
    str,
    GroundedActivityFocus,
] = {
    category: focus
    for focus, category in ACTIVITY_FOCUS_CATEGORY_PREFIXES.items()
    if category is not None
}


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
    activity_focus: GroundedActivityFocus | None = Field(
        description=(
            "Проверяемый вид конкретной активности или null для общей активности."
        ),
    )
    description: str | None = Field(
        min_length=1,
        max_length=1000,
        description=("Описание общей активности или null для конкретного места."),
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

        if has_place_id:
            if self.activity_focus is None or self.description is not None:
                raise ValueError(
                    "Для конкретного места activity_focus должен быть задан, "
                    "а description должен быть null."
                )
        elif self.activity_focus is not None or self.description is None:
            raise ValueError(
                "Для общей активности activity_focus должен быть null, "
                "а description должен быть задан."
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
        if activity.description is None:
            raise ValueError("General activity is missing a description.")

        return activity.description

    place = places_by_id.get(activity.source_place_id)

    if place is None:
        raise ValueError("Grounded activity refers to an unknown place ID.")

    if activity.activity_focus is None:
        raise ValueError("Grounded place activity is missing a focus.")

    formatted_description = ACTIVITY_FOCUS_DESCRIPTIONS[activity.activity_focus]

    if not formatted_description.endswith((".", "!", "?")):
        formatted_description = f"{formatted_description}."

    activity_parts = [f"{place.name}: {formatted_description}"]

    if place.location_source == "google":
        formatted_address = place.formatted_address

        if not formatted_address.endswith((".", "!", "?")):
            formatted_address = f"{formatted_address}."

        activity_parts.append(
            f"Актуальный адрес по данным Google Maps: {formatted_address}"
        )

    if place.opening_hours is not None:
        formatted_opening_hours = format_opening_hours(place.opening_hours)
        source_name = (
            "Google Maps" if place.opening_hours_source == "google" else "Geoapify"
        )
        activity_parts.append(
            f"Часы по данным {source_name}: {formatted_opening_hours}."
        )

    if place.website is not None:
        website_source = "Google Maps" if place.source == "google" else "Geoapify"
        activity_parts.append(f"Сайт из данных {website_source}: {place.website}")

    return " ".join(activity_parts)


def get_supported_activity_focuses(
    place: PlaceCandidate,
) -> set[GroundedActivityFocus]:
    """Возвращает проверяемые виды активности для категорий места."""

    supported_focuses: set[GroundedActivityFocus] = set()

    for focus, category in ACTIVITY_FOCUS_CATEGORY_PREFIXES.items():
        if category is None:
            continue

        category_prefix = f"{category}."

        if any(
            place_category == category or place_category.startswith(category_prefix)
            for place_category in place.categories
        ):
            supported_focuses.add(focus)

    if not supported_focuses:
        supported_focuses.add("place")

    return supported_focuses
