"""Проверяет структурированный отдых, объединение изменений и итоговый маршрут."""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import app.services.ai as ai
from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.grounded_trip import GroundedTripPlanResponse
from app.schemas.trip import (
    RestPeriodChange,
    TripEditAnalysis,
    TripPreferences,
)


def preferences(**updates: Any) -> TripPreferences:
    """Создаёт проверенные параметры, включая новые правила отдыха."""

    return TripPreferences.model_validate(
        {"destination": "Токио", "duration_days": 1, "interests": "Музеи", **updates}
    )


def rest(day: int | None, period: str) -> dict[str, Any]:
    return {"day": day, "period": period}


def change(day: int | None, period: str, enabled: bool) -> RestPeriodChange:
    return RestPeriodChange.model_validate({**rest(day, period), "rest": enabled})


def route(days: int = 1) -> tuple[TravelContext, GroundedTripPlanResponse]:
    """Модель предлагает обычную прогулку вечером, как в сообщении пользователя."""

    places = [
        PlaceCandidate(
            source_place_id=f"museum-{number}",
            name=f"Музей {number}",
            formatted_address="Тестовый адрес",
            latitude=35.68,
            longitude=139.76,
            categories=["entertainment.museum"],
            opening_hours="Mo-Su 09:00-17:00",
        )
        for number in range(1, days * 2 + 1)
    ]
    context = TravelContext(
        location=DestinationLocation(
            formatted_name="Токио",
            latitude=35.68,
            longitude=139.76,
            source_place_id="tokyo",
        ),
        requested_categories=["entertainment.museum"],
        places=places,
        fetched_at=datetime.now(UTC),
    )

    def visit(index: int) -> dict[str, Any]:
        place = places[index]
        return {
            "source_place_id": place.source_place_id,
            "place_name": place.name,
            "activity_focus": "museum",
            "description": None,
        }

    plan = GroundedTripPlanResponse.model_validate(
        {
            "destination": "Токио",
            "duration_days": days,
            "summary": "Непроверенная сводка.",
            "days": [
                {
                    "day": day,
                    "title": "Музеи",
                    "morning": [visit((day - 1) * 2)],
                    "afternoon": [visit((day - 1) * 2 + 1)],
                    "evening": [
                        {
                            "source_place_id": None,
                            "place_name": None,
                            "activity_focus": None,
                            "description": "прогулка",
                        }
                    ],
                }
                for day in range(1, days + 1)
            ],
        }
    )
    return context, plan


def validate(
    prefs: TripPreferences, context: TravelContext, plan: GroundedTripPlanResponse
):
    return ai._validate_grounded_trip_plan(
        plan.model_dump_json(), preferences=prefs, travel_context=context
    )


@pytest.mark.parametrize(
    "rules",
    [
        [rest(2, "evening")],
        [rest(None, "night")],
        [rest(None, "evening"), rest(None, "evening")],
        [rest(None, "evening"), rest(1, "evening")],
        [rest(True, "evening")],
    ],
)
def test_invalid_rest_preferences_are_rejected(rules) -> None:
    """Проверяет диапазон, типы, допустимые периоды и отсутствие повторов."""

    with pytest.raises(ValidationError):
        preferences(rest_periods=rules)


def test_old_preferences_without_rest_field_remain_valid() -> None:
    assert preferences().rest_periods == []


def test_edit_schema_requires_strict_rest_changes() -> None:
    schema = TripEditAnalysis.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    definition = schema["$defs"]["RestPeriodChange"]
    assert set(definition["required"]) == {"day", "period", "rest"}
    assert definition["additionalProperties"] is False
    assert definition["properties"]["period"]["enum"] == [
        "morning",
        "afternoon",
        "evening",
    ]


def test_cancel_single_day_preserves_other_days_and_unrelated_periods() -> None:
    original = preferences(
        duration_days=3, rest_periods=[rest(None, "evening"), rest(2, "morning")]
    )
    rules = original.rest_periods_after([change(2, "evening", False)])
    updated = preferences(duration_days=3, rest_periods=rules)
    assert updated.is_rest_period(1, "evening")
    assert not updated.is_rest_period(2, "evening")
    assert updated.is_rest_period(3, "evening")
    assert updated.is_rest_period(2, "morning")
    assert original.is_rest_period(2, "evening")
    assert updated.rest_periods_after([]) == rules


@pytest.mark.parametrize("reverse", [False, True])
def test_day_exception_overrides_general_change_in_any_order(reverse: bool) -> None:
    changes = [change(None, "evening", True), change(2, "evening", False)]
    if reverse:
        changes.reverse()
    prefs = preferences(duration_days=3)
    updated = preferences(
        duration_days=3, rest_periods=prefs.rest_periods_after(changes)
    )
    assert [updated.is_rest_period(day, "evening") for day in (1, 2, 3)] == [
        True,
        False,
        True,
    ]


def test_conflicting_changes_for_same_scope_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TripEditAnalysis(
            period_actions=[],
            place_replacements=[],
            requires_regeneration=True,
            regeneration_periods=[
                {"day": None, "period": period}
                for period in ("morning", "afternoon", "evening")
            ],
            supported=True,
            interests_changed=False,
            interests=None,
            must_visit_places_changed=False,
            must_visit_places=[],
            rest_period_changes=[
                change(None, "evening", True),
                change(None, "evening", False),
            ],
        )


def test_evening_rest_replaces_walk_and_refreshes_summary() -> None:
    context, plan = route()
    prefs = preferences(rest_periods=[rest(None, "evening")])
    result = validate(prefs, context, plan)
    assert result.days[0].evening == ["Спокойный отдых."]
    assert "Музей 1" in result.days[0].morning[0]
    assert "Музей 2" in result.days[0].afternoon[0]
    assert result.summary.endswith("Вечером: Спокойный отдых.")
    assert plan.days[0].evening[0].description == "прогулка"


def test_full_rest_day_does_not_relax_other_day_minimum() -> None:
    context, plan = route(2)
    context.places[-1].latitude += 0.1
    prefs = preferences(
        duration_days=2,
        rest_periods=[
            rest(2, period) for period in ("morning", "afternoon", "evening")
        ],
    )
    result = validate(prefs, context, plan)
    assert "Музей 1" in result.days[0].morning[0]
    for period in ("morning", "afternoon", "evening"):
        assert getattr(result.days[1], period) == ["Спокойный отдых."]
    requirements = ai._build_grounding_requirements(
        preferences=prefs, travel_context=context
    )
    assert requirements["minimum_places_per_day_by_day"] == {"1": 2, "2": 0}
    message = json.loads(
        ai._build_grounded_user_message(preferences=prefs, travel_context=context)
    )
    assert message["travel_context"]["geographic_planning"]["target_area_count"] == 1
    plan.days[0].afternoon = plan.days[0].evening
    with pytest.raises(ValueError, match="too few grounded places"):
        validate(prefs, context, plan)


def test_full_rest_trip_has_no_artificial_place_requirements() -> None:
    context, plan = route()
    prefs = preferences(
        rest_periods=[
            rest(None, period) for period in ("morning", "afternoon", "evening")
        ]
    )
    result = validate(prefs, context, plan)
    assert all(
        getattr(result.days[0], period) == ["Спокойный отдых."]
        for period in ("morning", "afternoon", "evening")
    )
    message = json.loads(
        ai._build_grounded_user_message(preferences=prefs, travel_context=context)
    )
    assert message["travel_context"]["places"] == []
    assert (
        message["travel_context"]["grounding_requirements"]["minimum_unique_places"]
        == 0
    )
    assert message["travel_context"]["interest_category_requirements"] == {}


def test_rest_outside_museum_hours_does_not_force_closed_visit() -> None:
    context, plan = route()
    prefs = preferences(rest_periods=[rest(None, "morning"), rest(None, "afternoon")])
    result = validate(prefs, context, plan)
    assert result.days[0].morning == ["Спокойный отдых."]
    assert result.days[0].afternoon == ["Спокойный отдых."]
    assert result.days[0].evening == ["прогулка"]


def test_rest_does_not_silently_remove_required_place() -> None:
    context, plan = route()
    context.places[0].opening_hours = "24/7"
    prefs = preferences(
        must_visit_places=["Музей 1"], rest_periods=[rest(None, "evening")]
    )
    plan.days[0].evening = plan.days[0].morning
    plan.days[0].morning = [plan.days[0].afternoon[0].model_copy(deep=True)]
    with pytest.raises(ValueError, match="omitted a required place"):
        validate(prefs, context, plan)


def test_required_visit_with_no_time_outside_rest_is_rejected_before_llm() -> None:
    context, _ = route()
    prefs = preferences(
        must_visit_places=["Музей 1"],
        rest_periods=[rest(None, "morning"), rest(None, "afternoon")],
    )
    with pytest.raises(ai.AIServiceError, match="вне выбранных периодов отдыха"):
        ai._build_grounded_user_message(preferences=prefs, travel_context=context)


def test_unknown_place_in_unreserved_period_is_still_rejected() -> None:
    context, plan = route()
    prefs = preferences(rest_periods=[rest(None, "evening")])
    plan.days[0].morning[0].source_place_id = "unknown-place"
    with pytest.raises(ValueError, match="outside travel context"):
        validate(prefs, context, plan)


@pytest.mark.asyncio
async def test_fully_reserved_trip_is_built_without_generation_llm(monkeypatch) -> None:
    """Для 30 дней готового отдыха нет риска обрезанного длинного ответа LLM."""

    context, _ = route()
    prefs = preferences(
        duration_days=30,
        rest_periods=[
            rest(None, period) for period in ("morning", "afternoon", "evening")
        ],
    )

    def unexpected_settings():
        pytest.fail("Параметры LLM не нужны для полностью заданного плана.")

    monkeypatch.setattr(ai, "get_settings", unexpected_settings)
    result = await ai.generate_trip_plan(preferences=prefs, travel_context=context)
    assert result.duration_days == 30
    assert len(result.days) == 30
    assert all(
        getattr(day, period) == ["Спокойный отдых."]
        for day in result.days
        for period in ("morning", "afternoon", "evening")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_first_day", [False, True])
async def test_edit_analysis_and_generation_apply_rest_without_extra_calls(
    monkeypatch, caplog, invalid_first_day: bool
) -> None:
    """Пропущенный моделью отдых исправляется после двух обычных этапов LLM."""

    context, plan = route()
    current = validate(preferences(), context, plan)
    requests: list[dict[str, Any]] = []
    analysis_data = {
        "period_actions": [],
        "place_replacements": [],
        "requires_regeneration": True,
        "regeneration_periods": [
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
        "supported": True,
        "interests_changed": False,
        "interests": None,
        "must_visit_places_changed": False,
        "must_visit_places": [],
        "rest_period_changes": [{"day": None, "period": "evening", "rest": True}],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        name = payload["response_format"]["json_schema"]["name"]
        response_analysis = analysis_data
        if name == "trip_edit_analysis" and invalid_first_day and len(requests) == 1:
            response_analysis = {
                **analysis_data,
                "rest_period_changes": [{"day": 2, "period": "evening", "rest": True}],
            }
        content = (
            json.dumps(response_analysis)
            if name == "trip_edit_analysis"
            else plan.model_dump_json()
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}]
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(
        ai,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.invalid/v1",
            llm_api_key="PRIVATE_API_KEY",
            llm_model="openai/gpt-oss-120b",
            llm_analysis_model="openai/gpt-oss-20b",
        ),
    )
    instruction = "Оставь музеи утром и днём. Вечером — спокойный отдых."
    caplog.set_level(logging.INFO, logger=ai.__name__)
    analysis = await ai.analyze_trip_edit(
        current_preferences=preferences(), current_plan=current, instruction=instruction
    )
    updated = preferences(
        rest_periods=preferences().rest_periods_after(analysis.rest_period_changes)
    )
    result = await ai.generate_edited_trip_plan(
        preferences=updated,
        current_preferences=preferences(),
        travel_context=context,
        current_plan=current,
        instruction=instruction,
    )
    assert len(requests) == (3 if invalid_first_day else 2)
    assert [request["model"] for request in requests] == (
        ["openai/gpt-oss-20b"] * (2 if invalid_first_day else 1)
        + ["openai/gpt-oss-120b"]
    )
    prompt = json.loads(requests[-1]["messages"][1]["content"])
    assert prompt["trip_preferences"]["rest_periods"] == [rest(None, "evening")]
    assert result.days[0].evening == ["Спокойный отдых."]
    assert "Музей 1" not in caplog.text
    assert "PRIVATE_API_KEY" not in caplog.text
    assert "Grounded rest periods applied | replacement_count=1" in caplog.text
