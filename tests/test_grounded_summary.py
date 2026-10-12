"""Проверяет сводку итогового плана без семантического разбора текста LLM."""

from datetime import UTC, datetime

import pytest

from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.grounded_trip import (
    GroundedActivity,
    GroundedDayPlan,
    GroundedTripPlanResponse,
)
from app.schemas.trip import TripPreferences
from app.services.ai import _validate_grounded_trip_plan


@pytest.fixture
def museum_case() -> tuple[TripPreferences, TravelContext, GroundedTripPlanResponse]:
    """Воспроизводит периоды из ответа бота; справочные данные здесь тестовые."""

    context = TravelContext(
        location=DestinationLocation(
            formatted_name="Токио",
            latitude=35.68,
            longitude=139.76,
            source_place_id="tokyo",
        ),
        requested_categories=["entertainment.museum"],
        places=[
            PlaceCandidate(
                name=name,
                formatted_address="Тестовый адрес, Токио",
                latitude=35.68,
                longitude=139.76,
                categories=["entertainment.museum"],
                source_place_id=place_id,
                opening_hours="Mo-Su 09:00-17:00",
            )
            for place_id, name in (
                ("aquarium", "Art Aquarium Museum GINZA"),
                ("aida", "Mitsuo Aida Museum"),
            )
        ],
        fetched_at=datetime.now(UTC),
    )
    plan = GroundedTripPlanResponse(
        destination="Токио",
        duration_days=1,
        summary="Однодневный маршрут с утренним, дневным и вечерним отдыхом.",
        days=[
            GroundedDayPlan(
                day=1,
                title="Музеи",
                morning=[
                    GroundedActivity(
                        source_place_id="aquarium",
                        place_name=context.places[0].name,
                        activity_focus="museum",
                        description=None,
                    )
                ],
                afternoon=[
                    GroundedActivity(
                        source_place_id="aida",
                        place_name=context.places[1].name,
                        activity_focus="museum",
                        description=None,
                    )
                ],
                evening=[
                    GroundedActivity(
                        source_place_id=None,
                        place_name=None,
                        activity_focus=None,
                        description="спокойно отдохнуть в уютном месте",
                    )
                ],
            )
        ],
    )
    return (
        TripPreferences(destination="Токио", duration_days=1, interests="Музеи"),
        context,
        plan,
    )


@pytest.mark.parametrize(
    "model_summary",
    [
        "Однодневный маршрут с утренним, дневным и вечерним отдыхом.",
        "Три обязательных парка и прогулка по набережной.",
        "Музеи работают до 22:00, билеты стоят 100 ₽: https://untrusted.example/.",
    ],
)
def test_one_day_summary_uses_correct_periods(museum_case, model_summary: str) -> None:
    """Посещения утром и днём не превращаются в отдых из сводки модели."""

    preferences, context, plan = museum_case
    plan.summary = model_summary
    result = _validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=preferences, travel_context=context
    )

    assert result.summary == (
        "Утром: посетить музей. Днём: посетить музей. "
        "Вечером: спокойно отдохнуть в уютном месте."
    )
    assert "https://" not in result.summary
    assert "22:00" not in result.summary
    assert "Art Aquarium Museum GINZA" in result.days[0].morning[0]
    assert "Mitsuo Aida Museum" in result.days[0].afternoon[0]
    assert "Часы по данным Geoapify" in result.days[0].afternoon[0]


def test_general_activity_keeps_its_actual_period(museum_case) -> None:
    """Свободное утро не означает, что конкретное вечернее посещение тоже отдых."""

    preferences, context, plan = museum_case
    day = plan.days[0]
    day.morning, day.evening = day.evening, day.morning
    context.places[0].opening_hours = "24/7"

    result = _validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=preferences, travel_context=context
    )
    assert result.summary == (
        "Утром: спокойно отдохнуть в уютном месте. "
        "Днём: посетить музей. Вечером: посетить музей."
    )


def test_summary_is_built_after_interest_focus_alignment(museum_case) -> None:
    """Сводка использует исправленный фокус, а не исходный выбор модели."""

    preferences, context, plan = museum_case
    preferences.interests = "Архитектура и музеи"
    context.places[0].categories.append("building.tourism")

    result = _validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=preferences, travel_context=context
    )
    assert result.summary.startswith("Утром: осмотреть архитектурный объект.")
    assert "Днём: посетить музей." in result.summary


def test_summary_includes_both_activities_in_a_period(museum_case) -> None:
    """Две активности одного периода сохраняются и не переносятся в соседний."""

    preferences, context, plan = museum_case
    plan.days[0].afternoon.append(plan.days[0].evening[0].model_copy(deep=True))
    result = _validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=preferences, travel_context=context
    )
    assert "Днём: посетить музей; спокойно отдохнуть в уютном месте." in result.summary


def test_multi_day_summary_deduplicates_verified_themes(museum_case) -> None:
    """Повторяющийся фокус не раздувает сводку, периоды разных дней не смешиваются."""

    preferences, context, plan = museum_case
    preferences.duration_days = plan.duration_days = 2
    plan.days.append(plan.days[0].model_copy(deep=True, update={"day": 2}))
    context.places[0].categories.append("building.historic")
    plan.days[1].morning[0].activity_focus = "history"

    result = _validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=preferences,
        travel_context=context,
        shorten_afternoons=False,
    )
    assert result.summary == "Темы маршрута: музеи и история."


def test_multi_day_general_plan_uses_neutral_summary(museum_case) -> None:
    """Пустой контекст не приводит к выдуманным музеям, прогулкам или районам."""

    preferences, context, plan = museum_case
    preferences.duration_days = plan.duration_days = 30
    context.places = []
    day = plan.days[0]
    general = day.evening[0]
    day.morning = [general.model_copy(deep=True)]
    day.afternoon = [general.model_copy(deep=True)]
    plan.days = [
        day.model_copy(deep=True, update={"day": number}) for number in range(1, 31)
    ]

    result = _validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=preferences, travel_context=context
    )
    assert result.summary == "План поездки по дням."
    assert len(result.days) == 30


def test_long_general_descriptions_are_shortened_only_in_summary(museum_case) -> None:
    """Длинные активности не превращают краткую сводку в переполненное сообщение."""

    preferences, context, plan = museum_case
    context.places = []
    description = ("Спокойная прогулка по окрестностям. " * 25).strip()
    general = GroundedActivity(
        source_place_id=None,
        place_name=None,
        activity_focus=None,
        description=description,
    )
    for period in ("morning", "afternoon", "evening"):
        setattr(plan.days[0], period, [general.model_copy(deep=True) for _ in range(2)])

    result = _validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=preferences, travel_context=context
    )
    assert len(result.summary) < 1_300
    assert "…" in result.summary
    assert "Утром:" in result.summary
    assert "Днём:" in result.summary
    assert "Вечером:" in result.summary
    assert result.days[0].morning == [description, description]
    assert result.days[0].afternoon == [description, description]
    assert result.days[0].evening == [description, description]
