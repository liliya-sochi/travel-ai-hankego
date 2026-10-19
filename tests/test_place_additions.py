"""Именованные добавления сохраняют прежние посещения без полной генерации."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.grounded_trip import REST_PERIOD_DESCRIPTION
from app.schemas.trip import RestPeriod, TripEditAnalysis
from app.services import ai
from scripts import evaluate_routes as evaluation


def addition_analysis(**updates):
    """Разбирает фиксированное добавление; остальные поля не меняются."""

    return TripEditAnalysis.model_validate(
        {
            "supported": True,
            "interests_changed": False,
            "interests": None,
            "must_visit_places_changed": True,
            "must_visit_places": ["Art Aquarium Museum GINZA", "Тестовый музей 5"],
            "rest_period_changes": [],
            "period_actions": [],
            "requires_regeneration": False,
            "regeneration_periods": [],
            "place_replacements": [],
            "place_additions": [
                {
                    "day": 2,
                    "period": "afternoon",
                    "place_name": "Тестовый музей 5",
                    "activity_focus": "museum",
                }
            ],
            **updates,
        }
    )


@pytest.fixture
def addition_case():
    suite = evaluation.load_edit_suite()
    case = suite.cases[3]
    return case, suite.plans[case.current_plan], suite.contexts[case.context]


async def generate(case, plan, context, parsed):
    updated = evaluation.merge_edit_preferences(case.preferences, parsed)
    return await ai.generate_edited_trip_plan(
        preferences=updated,
        current_preferences=case.preferences,
        current_plan=plan,
        travel_context=context,
        instruction=case.steps[0].user_message,
        analysis=parsed,
    )


@pytest.mark.asyncio
async def test_named_addition_keeps_all_old_visits_and_never_calls_generation(
    addition_case, monkeypatch
):
    case, plan, context = addition_case
    before = [item.model_dump_json() for item in (case, plan, context)]
    generator = AsyncMock(side_effect=AssertionError("Генерация не нужна"))
    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    monkeypatch.setattr(
        ai, "get_settings", lambda: pytest.fail("Настройки генерации не нужны")
    )
    result = await generate(case, plan, context, addition_analysis())
    assert all(
        evaluation.assess_edit_periods(
            result, case, case.steps[0].expectations, context
        ).values()
    )
    assert [item.model_dump_json() for item in (case, plan, context)] == before
    generator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "full",
        "missing_new",
        "ambiguous",
        "wrong_focus",
        "closed_new",
        "missing_old",
        "closed_old",
        "rest",
        "evening_unknown",
        "replacement_conflict",
    ],
)
async def test_addition_failure_is_atomic_and_does_not_generate(
    addition_case, monkeypatch, defect
):
    case, plan, context = addition_case
    parsed = addition_analysis()
    old = next(p for p in context.places if p.source_place_id == "eval-edit-museum-4")
    new = next(p for p in context.places if p.source_place_id == "eval-edit-museum-5")
    if defect == "full":
        plan.days[1].afternoon.append("Неспешная пауза.")
    elif defect == "missing_new":
        context.places.remove(new)
    elif defect == "ambiguous":
        context.places.append(new.model_copy(update={"source_place_id": "duplicate"}))
    elif defect == "wrong_focus":
        new.categories = ["leisure.park"]
    elif defect == "closed_new":
        new.opening_hours = "Mo-Su 00:00-01:00"
    elif defect == "missing_old":
        context.places.remove(old)
    elif defect == "closed_old":
        old.opening_hours = "Mo-Su 00:00-01:00"
    elif defect == "rest":
        case.preferences.rest_periods = [RestPeriod(day=2, period="afternoon")]
        plan.days[1].afternoon = [REST_PERIOD_DESCRIPTION]
    elif defect == "evening_unknown":
        parsed.place_additions[0].period = "evening"
        new.opening_hours = None
    else:
        parsed = addition_analysis(
            requires_regeneration=True,
            regeneration_periods=[{"day": 2, "period": "afternoon"}],
            place_replacements=[
                {
                    "day": 2,
                    "period": "afternoon",
                    "place_name": old.name,
                    "activity_focus": "museum",
                    "nearby": False,
                }
            ],
        )
    before = [item.model_dump_json() for item in (case, plan, context, parsed)]
    generator = AsyncMock(
        side_effect=AssertionError("Ошибку надо обнаружить до генерации")
    )
    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    with pytest.raises(
        ai.AIServiceError, match=r"Маршрут не изменён|маршрут не изменён"
    ):
        await generate(case, plan, context, parsed)
    generator.assert_not_awaited()
    assert [item.model_dump_json() for item in (case, plan, context, parsed)] == before


@pytest.mark.asyncio
async def test_cancelled_rest_is_filled_with_named_visit_locally(addition_case):
    case, plan, context = addition_case
    case.preferences = case.preferences.model_copy(update={"rest_periods": []})
    case.preferences.rest_periods = [RestPeriod(day=2, period="afternoon")]
    plan.days[1].afternoon = [REST_PERIOD_DESCRIPTION]
    parsed = addition_analysis(
        rest_period_changes=[{"day": 2, "period": "afternoon", "rest": False}]
    )
    result = await generate(case, plan, context, parsed)
    assert len(result.days[1].afternoon) == 1
    assert result.days[1].afternoon[0].startswith("Тестовый музей 5: посетить музей.")
    assert result.days[1].evening == plan.days[1].evening


@pytest.mark.asyncio
async def test_quiet_walk_and_addition_share_period(addition_case):
    case, plan, context = addition_case
    parsed = addition_analysis(
        period_actions=[{"day": 2, "period": "afternoon", "action": "quiet_walk"}]
    )
    result = await generate(case, plan, context, parsed)
    assert result.days[1].afternoon[0] == "Спокойная прогулка."
    assert result.days[1].afternoon[1].startswith("Тестовый музей 5:")


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting_focus", [False, True])
async def test_already_present_addition_never_duplicates_or_changes_action(
    addition_case, conflicting_focus
):
    case, plan, context = addition_case
    plan.days[1].afternoon = ["Тестовый музей 5: посетить музей."]
    parsed = addition_analysis()
    if conflicting_focus:
        new = next(
            p for p in context.places if p.source_place_id == "eval-edit-museum-5"
        )
        new.categories.append("building.tourism")
        parsed.place_additions[0].activity_focus = "architecture"
        with pytest.raises(ai.AIServiceError, match="другим действием"):
            await generate(case, plan, context, parsed)
    else:
        result = await generate(case, plan, context, parsed)
        assert len(result.days[1].afternoon) == 1
        assert result.days[1].afternoon[0].startswith(plan.days[1].afternoon[0])


@pytest.mark.parametrize(
    "defect",
    [
        "removal",
        "unrelated_required",
        "not_required",
        "absent_instruction",
        "outside_day",
    ],
)
def test_analysis_cannot_hide_other_mandatory_changes_in_local_addition(
    addition_case, defect
):
    case, plan, _ = addition_case
    parsed = addition_analysis()
    instruction = case.steps[0].user_message
    if defect == "removal":
        parsed.must_visit_places = ["Тестовый музей 5"]
        instruction += " Art Aquarium Museum GINZA"
    elif defect == "unrelated_required":
        parsed.must_visit_places.append("Другой музей")
        instruction += " Другой музей"
    elif defect == "not_required":
        parsed.must_visit_places = case.preferences.must_visit_places
        parsed.must_visit_places_changed = False
    elif defect == "absent_instruction":
        instruction = "Добавь музей"
    else:
        parsed.place_additions[0].day = 3
    with pytest.raises(ValueError):
        ai._validate_trip_edit_analysis(
            analysis=parsed,
            current_preferences=case.preferences,
            current_plan=plan,
            instruction=instruction,
        )


def test_addition_schema_requires_all_fields_and_unique_requests():
    payload = addition_analysis().model_dump()
    del payload["place_additions"]
    with pytest.raises(ValidationError):
        TripEditAnalysis.model_validate(payload)
    payload = addition_analysis().model_dump()
    payload["place_additions"].append(deepcopy(payload["place_additions"][0]))
    with pytest.raises(ValidationError):
        TripEditAnalysis.model_validate(payload)


@pytest.mark.asyncio
async def test_addition_is_fixed_during_generation_in_another_period(
    addition_case, monkeypatch
):
    case, plan, context = addition_case
    parsed = addition_analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": 1, "period": "evening"}],
    )
    captured = {}

    async def generator(**arguments):
        captured.update(arguments)
        return plan

    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    await generate(case, plan, context, parsed)
    assert [
        a.source_place_id for a in captured["preserved_activities"][2, "afternoon"]
    ] == ["eval-edit-museum-4", "eval-edit-museum-5"]
    assert (1, "evening") not in captured["preserved_activities"]


@pytest.mark.asyncio
async def test_one_day_park_interests_keep_park_and_append_required_museum(
    addition_case,
):
    """Регрессия строгого дневного фильтра: новая обязательная тема разрешена локально."""

    case, plan, context = addition_case
    case.preferences = case.preferences.model_copy(
        update={"duration_days": 1, "interests": "Парки"}
    )
    plan = plan.model_copy(
        deep=True, update={"duration_days": 1, "days": plan.days[:1]}
    )
    day_park = next(p for p in context.places if p.source_place_id == "eval-edit-aida")
    day_park.categories = ["leisure.park"]
    day_park.opening_hours = "24/7"
    evening_park = next(
        p for p in context.places if p.source_place_id == "eval-edit-near-park"
    )
    evening_park.opening_hours = "24/7"
    context.places = [
        p
        for p in context.places
        if p.source_place_id
        in {
            "eval-edit-aquarium",
            "eval-edit-aida",
            "eval-edit-near-park",
            "eval-edit-museum-5",
        }
    ]
    plan.days[0].afternoon = [f"{day_park.display_name}: прогуляться по парку."]
    plan.days[0].evening = [f"{evening_park.display_name}: прогуляться по парку."]
    parsed = addition_analysis()
    parsed.place_additions[0].day = 1
    result = await generate(case, plan, context, parsed)
    assert len(result.days[0].afternoon) == 2
    assert result.days[0].afternoon[0].startswith(plan.days[0].afternoon[0])
    assert result.days[0].afternoon[1].startswith("Тестовый музей 5: посетить музей.")
    assert result.days[0].evening[0].startswith(plan.days[0].evening[0])
