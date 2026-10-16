"""Проверки области отдыха по сбою двухдневного edit-eval."""

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.schemas.grounded_trip import (
    REST_PERIOD_DESCRIPTION,
    GroundedActivity,
    GroundedTripPlanResponse,
)
from app.schemas.trip import TripPreferences
from app.services import ai
from scripts import evaluate_routes as evaluation


@pytest.fixture
def suite():
    return evaluation.load_edit_suite()


def scoped_preferences(current: TripPreferences) -> TripPreferences:
    """Резервирует только второй вечер, как в фактическом отчёте."""

    return TripPreferences.model_validate(
        {
            **current.model_dump(),
            "rest_periods": [{"day": 2, "period": "evening"}],
        }
    )


def model_plan(suite) -> GroundedTripPlanResponse:
    """Сохраняет четыре исходных музея и две общие прогулки."""

    case = suite.cases[1]
    context = suite.contexts[case.context]
    by_id = {place.source_place_id: place for place in context.places}
    days: list[dict[str, Any]] = []
    for day in (1, 2):
        visits = {
            item.period: [
                {
                    "source_place_id": place_id,
                    "place_name": by_id[place_id].name,
                    "activity_focus": "museum",
                    "description": None,
                }
                for place_id in item.place_ids
            ]
            for item in case.preserved_visits
            if item.day == day
        }
        days.append(
            {
                "day": day,
                "title": "План дня",
                **visits,
                "evening": [general("Спокойная прогулка.")],
            }
        )
    return GroundedTripPlanResponse(
        destination="Токио", duration_days=2, summary="План поездки", days=days
    )


def general(description: str) -> GroundedActivity:
    return GroundedActivity(
        source_place_id=None,
        place_name=None,
        activity_focus=None,
        description=description,
    )


def validate(suite, plan, *, current=None, updated=None, saved=None):
    case = suite.cases[1]
    return ai._validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=updated or scoped_preferences(case.preferences),
        current_preferences=current or case.preferences,
        current_plan=saved or suite.plans[case.current_plan],
        travel_context=suite.contexts[case.context],
        shorten_afternoons=False,
    )


@pytest.mark.parametrize(
    "description", ["Спокойный отдых.", "спокойный отдых", "  СПОКОЙНЫЙ   ОТДЫХ! "]
)
@pytest.mark.parametrize("cell", [(1, "evening"), (1, "morning"), (2, "afternoon")])
def test_rejects_rest_spilling_to_another_day_or_period(suite, description, cell):
    """Наличие правильного второго вечера не разрешает отдых в других клетках."""

    plan = model_plan(suite)
    day, period = cell
    setattr(plan.days[day - 1], period, [general(description)])
    before = suite.model_dump_json()
    with pytest.raises(ValueError, match="outside its edited scope"):
        validate(suite, plan)
    assert suite.model_dump_json() == before


def test_correct_scope_formats_rest_without_changing_first_evening(suite):
    plan = validate(suite, model_plan(suite))
    assert plan.days[0].evening == ["Спокойная прогулка."]
    assert plan.days[1].evening == [REST_PERIOD_DESCRIPTION]
    case = suite.cases[1]
    checks = evaluation.assess_edit_periods(
        plan, case, case.steps[0].expectations, suite.contexts[case.context]
    )
    assert all(checks.values()), checks


def test_cancellation_rejects_rest_even_when_no_rules_remain(suite):
    case = suite.cases[1]
    current = scoped_preferences(case.preferences)
    saved = validate(suite, model_plan(suite))
    plan = model_plan(suite)
    plan.days[1].evening = [general(REST_PERIOD_DESCRIPTION)]
    with pytest.raises(ValueError, match="outside its edited scope"):
        validate(suite, plan, current=current, updated=case.preferences, saved=saved)
    result = validate(
        suite, model_plan(suite), current=current, updated=case.preferences, saved=saved
    )
    assert all(day.evening == ["Спокойная прогулка."] for day in result.days)


def test_preserves_old_optional_rest_without_promoting_it_to_a_rule(suite):
    case = suite.cases[1]
    saved = suite.plans[case.current_plan].model_copy(deep=True)
    saved.days[0].evening = [REST_PERIOD_DESCRIPTION]
    plan = model_plan(suite)
    plan.days[0].evening = [general(REST_PERIOD_DESCRIPTION)]
    updated = scoped_preferences(case.preferences)
    result = validate(suite, plan, saved=saved, updated=updated)
    assert all(day.evening == [REST_PERIOD_DESCRIPTION] for day in result.days)
    assert [rule.day for rule in updated.rest_periods] == [2]


def test_creation_and_edits_without_structured_rules_allow_optional_rest(suite):
    case = suite.cases[1]
    plan = model_plan(suite)
    plan.days[0].evening = [general(REST_PERIOD_DESCRIPTION)]
    result = validate(suite, plan, updated=case.preferences)
    created = ai._validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=case.preferences,
        travel_context=suite.contexts[case.context],
        shorten_afternoons=False,
    )
    assert result == created
    assert created.days[0].evening == [REST_PERIOD_DESCRIPTION]


def test_scope_check_does_not_restore_unavailable_saved_visits(suite):
    """При правильном отдыхе по-прежнему запрещены отсутствующие свежие ID."""

    plan = model_plan(suite)
    plan.days[0].morning[0].source_place_id = "stale-id"
    with pytest.raises(ValueError, match="outside travel context"):
        validate(suite, plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaust", [False, True])
@pytest.mark.parametrize("fault_turn", [1, 2, 3])
async def test_edit_eval_retries_scope_error_and_keeps_actual_state(
    suite, monkeypatch, caplog, exhaust, fault_turn
):
    """Реальные AI, merge и eval не принимают ответ с отдыхом в обоих днях."""

    case = suite.cases[1]
    requests: list[dict[str, Any]] = []
    turn = 0
    generation_attempt = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal turn, generation_attempt
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["response_format"]["json_schema"]["name"] == "trip_edit_analysis":
            turn += 1
            generation_attempt = 0
            content = {
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
                "must_visit_places": ["Art Aquarium Museum GINZA"],
                "rest_period_changes": (
                    []
                    if turn == 2
                    else [{"day": 2, "period": "evening", "rest": turn == 1}]
                ),
            }
        else:
            generation_attempt += 1
            plan = model_plan(suite)
            if turn == fault_turn and (generation_attempt == 1 or exhaust):
                for day in plan.days:
                    if turn != 3 or day.day == 2:
                        day.evening = [general(REST_PERIOD_DESCRIPTION)]
            content = plan.model_dump(mode="json")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(content, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="PRIVATE_API_KEY",
        llm_model="fixture-generation",
        llm_analysis_model="fixture-analysis",
    )
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    caplog.set_level(logging.INFO, logger=ai.__name__)
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == ("service_error" if exhaust else "passed"), result
    assert len(requests) == (2 * fault_turn + 1 if exhaust else 7)
    calls = result["steps"][fault_turn - 1]["calls"]
    assert calls[1]["validation_reason"] == "unexpected_rest_period"
    assert calls[1]["outcome"] == "semantic_validation_failed"
    assert calls[2]["attempt"] == 2
    assert "Не распространяй" in requests[2 * fault_turn]["messages"][-1]["content"]
    assert "PRIVATE_API_KEY" not in caplog.text
    assert "Art Aquarium" not in caplog.text
    if exhaust:
        assert result["failed_step"] == fault_turn
        assert "plan" not in result["steps"][-1]
        assert all("plan" in step for step in result["steps"][:-1])
        assert calls[2]["validation_reason"] == "unexpected_rest_period"
    else:
        assert len(result["steps"]) == 3
        assert result["steps"][0]["plan"]["days"][0]["evening"] == [
            "Спокойная прогулка."
        ]
        for previous, next_step in zip(
            result["steps"][:-1], result["steps"][1:], strict=True
        ):
            assert next_step["input_preferences"] == previous["updated_preferences"]
            assert next_step["input_plan"] == previous["plan"]
        # После отмены генератор видит и старое правило, и пустой итоговый список.
        cancelled_input = json.loads(requests[-1]["messages"][1]["content"])
        assert cancelled_input["current_rest_periods"] == [
            {"day": 2, "period": "evening"}
        ]
        assert cancelled_input["trip_preferences"]["rest_periods"] == []
