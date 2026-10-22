"""Удаление одного названного посещения сохраняет остальные и не требует генерации."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.trip import TripEditAnalysis
from app.services import ai
from app.services.ai_diagnostics import analysis_validation_details
from app.services.trip_editing import (
    context_without_removed_visits,
    plan_without_removed_visits,
    preserved_place_names,
    removed_visit_names,
)
from scripts import evaluate_routes as evaluation


def removal_analysis(**updates):
    """Фиксирует удаление только пятого музея второго дня днём."""

    return TripEditAnalysis.model_validate(
        {
            "supported": True,
            "interests_changed": False,
            "interests": None,
            "must_visit_places_changed": True,
            "must_visit_places": ["Art Aquarium Museum GINZA"],
            "rest_period_changes": [],
            "period_actions": [],
            "requires_regeneration": False,
            "regeneration_periods": [],
            "place_replacements": [],
            "place_additions": [],
            "place_moves": [],
            "place_removals": [
                {"day": 2, "period": "afternoon", "place_name": "Тестовый музей 5"}
            ],
            **updates,
        }
    )


@pytest.fixture
def removal_case():
    suite = evaluation.load_edit_suite()
    case = suite.cases[4]
    return case, suite.plans[case.current_plan], suite.contexts[case.context]


async def generate(case, plan, context, parsed):
    return await ai.generate_edited_trip_plan(
        preferences=evaluation.merge_edit_preferences(case.preferences, parsed),
        current_preferences=case.preferences,
        current_plan=plan,
        travel_context=context,
        instruction=case.steps[0].user_message,
        analysis=parsed,
    )


def validate(case, plan, parsed, instruction=None):
    ai._validate_trip_edit_analysis(
        analysis=parsed,
        current_preferences=case.preferences,
        current_plan=plan,
        instruction=instruction or case.steps[0].user_message,
    )


@pytest.mark.asyncio
async def test_removal_keeps_other_visit_focus_walks_and_never_generates(
    removal_case, monkeypatch
):
    case, plan, context = removal_case
    before = [item.model_dump_json() for item in (case, plan, context)]
    parsed = removal_analysis()
    validate(case, plan, parsed)
    monkeypatch.setattr(
        ai, "get_settings", lambda: pytest.fail("Полная генерация не нужна")
    )
    generator = AsyncMock(side_effect=AssertionError("Генерация не нужна"))
    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    result = await generate(case, plan, context, parsed)
    assert len(result.days[1].afternoon) == 1
    assert result.days[1].afternoon[0].startswith("Тестовый музей 4: посетить музей.")
    assert (
        result.days[0]
        .afternoon[0]
        .startswith("Mitsuo Aida Museum: осмотреть архитектурный объект.")
    )
    for index in range(2):
        assert result.days[index].evening == plan.days[index].evening
    assert all(
        evaluation.assess_edit_periods(
            result, case, case.steps[0].expectations, context
        ).values()
    )
    assert [item.model_dump_json() for item in (case, plan, context)] == before
    assert evaluation.merge_edit_preferences(
        case.preferences, parsed
    ).must_visit_places == ["Art Aquarium Museum GINZA"]
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_visit_removal_keeps_required_neighbor(removal_case):
    case, plan, context = removal_case
    parsed = removal_analysis(
        must_visit_places_changed=False,
        must_visit_places=case.preferences.must_visit_places,
        place_moves=[],
        place_removals=[
            {"day": 2, "period": "afternoon", "place_name": "Тестовый музей 4"}
        ],
    )
    validate(
        case, plan, parsed, "Днём второго дня удали Тестовый музей 4. Остальное оставь."
    )
    result = await generate(case, plan, context, parsed)
    assert len(result.days[1].afternoon) == 1
    assert result.days[1].afternoon[0].startswith("Тестовый музей 5: посетить музей.")


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing", "closed", "ambiguous", "wrong_category"])
async def test_removed_place_does_not_need_fresh_confirmation(removal_case, defect):
    case, plan, context = removal_case
    old = next(p for p in context.places if p.source_place_id == "eval-edit-museum-5")
    if defect == "missing":
        context.places.remove(old)
    elif defect == "closed":
        old.opening_hours = "Mo-Su 00:00-01:00"
    elif defect == "ambiguous":
        context.places.append(
            old.model_copy(update={"source_place_id": "another-old-id"})
        )
    else:
        old.categories = ["leisure.park"]
    result = await generate(case, plan, context, removal_analysis())
    assert len(result.days[1].afternoon) == 1
    assert "Тестовый музей 4" in result.days[1].afternoon[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", ["missing", "closed", "ambiguous", "wrong_category", "empty_period"]
)
async def test_removal_failure_is_atomic_before_any_generation(
    removal_case, monkeypatch, defect
):
    case, plan, context = removal_case
    kept = next(p for p in context.places if p.source_place_id == "eval-edit-museum-4")
    if defect == "missing":
        context.places.remove(kept)
    elif defect == "closed":
        kept.opening_hours = "Mo-Su 00:00-01:00"
    elif defect == "ambiguous":
        context.places.append(
            kept.model_copy(update={"source_place_id": "another-kept-id"})
        )
    elif defect == "wrong_category":
        kept.categories = ["leisure.park"]
    else:
        plan.days[1].afternoon = plan.days[1].afternoon[1:]
    before = [item.model_dump_json() for item in (case, plan, context)]
    generator = AsyncMock(side_effect=AssertionError("Генерация не нужна"))
    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    with pytest.raises(ai.AIServiceError, match="Маршрут не изменён"):
        await generate(case, plan, context, removal_analysis())
    assert [item.model_dump_json() for item in (case, plan, context)] == before
    generator.assert_not_awaited()


@pytest.mark.parametrize(
    ("defect", "reason"),
    [
        ("unknown", "removal_reference_not_unique"),
        ("wrong_period", "removal_reference_not_unique"),
        ("outside_trip", "removal_day_outside_trip"),
        ("absent_instruction", "removal_not_explicit"),
        ("still_required", "removed_visit_still_required"),
        ("undeclared", "undeclared_required_change"),
        ("unrelated_required", "required_change_absent_from_instruction"),
        ("unrelated_explicit_required", "local_required_change_beyond_visits"),
        ("generation_conflict", "removal_scope_conflict"),
        ("walk_conflict", "removal_scope_conflict"),
        ("rest_conflict", "removal_scope_conflict"),
        ("addition_conflict", "removal_scope_conflict"),
        ("replacement_conflict", "removal_scope_conflict"),
        ("duplicate_alias", "duplicate_removal_reference"),
        ("ambiguous_saved", "removal_reference_not_unique"),
        ("no_plan", "removal_plan_unavailable"),
    ],
)
def test_analysis_rejects_wrong_removal_scope_identity_and_required_changes(
    removal_case, defect, reason
):
    case, plan, _ = removal_case
    parsed = removal_analysis()
    instruction = case.steps[0].user_message
    if defect == "unknown":
        parsed.place_removals[0].place_name = "Неизвестный музей"
    elif defect == "wrong_period":
        parsed.place_removals[0].period = "morning"
    elif defect == "outside_trip":
        parsed.place_removals[0].day = 3
    elif defect == "absent_instruction":
        instruction = "Все музеи оставь."
        parsed.must_visit_places = case.preferences.must_visit_places
    elif defect == "still_required":
        parsed.must_visit_places = case.preferences.must_visit_places
    elif defect == "undeclared":
        parsed.must_visit_places_changed = False
    elif defect.startswith("unrelated"):
        parsed.must_visit_places = []
        if defect == "unrelated_explicit_required":
            instruction += " Art Aquarium Museum GINZA"
    elif defect == "generation_conflict":
        parsed.requires_regeneration = True
        parsed.regeneration_periods = [
            evaluation.TripPeriodScope(day=2, period="afternoon")
        ]
    elif defect == "walk_conflict":
        parsed = removal_analysis(
            period_actions=[
                {"day": None, "period": "afternoon", "action": "quiet_walk"}
            ]
        )
    elif defect == "rest_conflict":
        parsed = removal_analysis(
            rest_period_changes=[{"day": 2, "period": "afternoon", "rest": True}]
        )
    elif defect == "addition_conflict":
        parsed = removal_analysis(
            place_additions=[
                {
                    "day": 2,
                    "period": "afternoon",
                    "place_name": "Тестовый музей 4",
                    "activity_focus": None,
                }
            ]
        )
    elif defect == "replacement_conflict":
        parsed = removal_analysis(
            requires_regeneration=True,
            regeneration_periods=[{"day": 2, "period": "afternoon"}],
            place_replacements=[
                {
                    "day": 2,
                    "period": "afternoon",
                    "place_name": "Тестовый музей 4",
                    "activity_focus": None,
                    "nearby": False,
                }
            ],
        )
    elif defect == "duplicate_alias":
        parsed = removal_analysis(
            place_moves=[],
            place_removals=[
                {"day": 2, "period": "afternoon", "place_name": "Тестовый музей 5"},
                {"day": 2, "period": "afternoon", "place_name": "ТЕСТОВЫЙ МУЗЕЙ 5!"},
            ],
        )
    elif defect == "ambiguous_saved":
        plan.days[1].afternoon = [plan.days[1].afternoon[1]] * 2
    else:
        plan = None
    with pytest.raises(ValueError) as caught:
        validate(case, plan, parsed, instruction)
    details = analysis_validation_details(caught.value, stage="semantic")
    assert details["validation_reason"] == reason
    assert "музей" not in str(details)


def test_removal_schema_is_required_strict_and_disallows_duplicates():
    payload = removal_analysis().model_dump()
    del payload["place_removals"]
    with pytest.raises(ValidationError):
        TripEditAnalysis.model_validate(payload)
    payload = removal_analysis().model_dump()
    payload["place_removals"].append(deepcopy(payload["place_removals"][0]))
    with pytest.raises(ValidationError) as caught:
        TripEditAnalysis.model_validate(payload)
    assert (
        analysis_validation_details(caught.value, stage="schema")["validation_reason"]
        == "duplicate_removals"
    )
    for field, value in [
        ("day", None),
        ("day", "2"),
        ("period", "night"),
        ("place_name", ""),
    ]:
        payload = removal_analysis().model_dump()
        payload["place_removals"][0][field] = value
        with pytest.raises(ValidationError):
            TripEditAnalysis.model_validate(payload)


@pytest.mark.asyncio
async def test_preserve_action_for_period_keeps_only_surviving_visit(removal_case):
    case, plan, context = removal_case
    parsed = removal_analysis(
        period_actions=[{"day": None, "period": "afternoon", "action": "preserve"}]
    )
    validate(case, plan, parsed)
    result = await generate(case, plan, context, parsed)
    assert len(result.days[1].afternoon) == 1
    assert "Тестовый музей 4" in result.days[1].afternoon[0]


def test_lookup_excludes_deleted_name_and_accepts_display_alias(removal_case):
    case, plan, context = removal_case
    plan.days[1].afternoon[1] = "Тестовый музей 5 (Archive Five): посетить музей."
    parsed = removal_analysis()
    validate(case, plan, parsed)
    retained = plan_without_removed_visits(plan, parsed)
    names = preserved_place_names(retained, {(2, "afternoon")})
    assert names == ["Тестовый музей 4"]
    filtered = context_without_removed_visits(
        context, removed_visit_names(plan, parsed)
    )
    assert "eval-edit-museum-5" not in [p.source_place_id for p in filtered.places]


@pytest.mark.asyncio
async def test_mixed_generation_locks_trimmed_period_and_excludes_removed_id(
    removal_case, monkeypatch
):
    case, plan, context = removal_case
    parsed = removal_analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": 1, "period": "morning"}],
    )
    captured = {}

    async def generator(**arguments):
        captured.update(arguments)
        return plan

    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    await generate(case, plan, context, parsed)
    assert [
        a.source_place_id for a in captured["preserved_activities"][2, "afternoon"]
    ] == ["eval-edit-museum-4"]
    assert (1, "morning") not in captured["preserved_activities"]
    assert "eval-edit-museum-5" not in [
        p.source_place_id for p in captured["travel_context"].places
    ]


@pytest.mark.parametrize("first_invalid", [False, True])
def test_actual_analyzer_http_and_eval_report_skip_generation(
    removal_case, monkeypatch, tmp_path, first_invalid
):
    """Настоящие HTTP-разбор, семантика, локальная правка и запись eval-отчёта."""

    import json
    from types import SimpleNamespace

    import httpx

    case, _, _ = removal_case
    valid = removal_analysis().model_dump()
    replies = [valid]
    if first_invalid:
        invalid = deepcopy(valid)
        del invalid["place_removals"][0]["period"]
        replies.insert(0, invalid)
    calls = []

    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "fixture-analysis"
        schema = payload["response_format"]["json_schema"]["schema"]
        assert "place_removals" in schema["required"]
        incoming = json.loads(payload["messages"][1]["content"])
        assert "expectations" not in incoming
        assert "trimmed_periods" not in incoming
        assert incoming["edit_instruction"] == case.steps[0].user_message
        return httpx.Response(
            200,
            json={
                "model": "fixture-analysis",
                "choices": [
                    {
                        "message": {"content": json.dumps(replies.pop(0))},
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

    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="fixture-secret",
        llm_model="fixture-generation",
        llm_analysis_model="fixture-analysis",
    )
    client = httpx.AsyncClient
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs),
    )
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    report_path = tmp_path / "removal.json"
    code = evaluation.main(
        [
            "--suite",
            "edits",
            "--live",
            "--case",
            case.id,
            "--repeat",
            "1",
            "--output",
            str(report_path),
        ]
    )
    report = json.loads(report_path.read_text())
    assert code == 0 and report["summary"] == {"passed": 1}
    assert len(calls) == 1 + first_invalid
    assert report["results"][0]["steps"][0]["checks"]["generation_expected"]
    if first_invalid:
        diagnostic = report["results"][0]["calls"][0]
        assert diagnostic["validation_stage"] == "schema"
        assert diagnostic["validation_issues"][0]["field"] == "place_removals[].period"
    assert "fixture-secret" not in report_path.read_text()
    assert not replies


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defect", "failed_check"),
    [
        ("unchanged_period", "removal:2:afternoon"),
        ("removed_elsewhere", "removed_absent:2:afternoon"),
        ("wrong_target_focus", "removal_focus:2:afternoon"),
        ("wrong_preserved_focus", "preserved_focus:1:afternoon"),
    ],
)
async def test_independent_removal_eval_rejects_bad_output(
    removal_case, defect, failed_check
):
    case, plan, context = removal_case
    result = await generate(case, plan, context, removal_analysis())
    if defect == "unchanged_period":
        result.days[1].afternoon = plan.days[1].afternoon
    elif defect == "removed_elsewhere":
        result.days[0].evening = plan.days[1].afternoon[1:]
    elif defect == "wrong_target_focus":
        result.days[1].afternoon = ["Тестовый музей 4: осмотреть архитектурный объект."]
    else:
        result.days[0].afternoon = ["Mitsuo Aida Museum: посетить музей."]
    checks = evaluation.assess_edit_periods(
        result, case, case.steps[0].expectations, context
    )
    assert not checks[failed_check]


@pytest.mark.parametrize(
    "defect",
    [
        "day",
        "missing_request",
        "missing_output",
        "overlap_ids",
        "unknown_removed_id",
        "overlap_scope",
    ],
)
def test_removal_eval_corpus_rejects_inconsistent_criteria(defect):
    data = evaluation.load_edit_suite().model_dump(mode="json")
    step = data["cases"][4]["steps"][0]["expectations"]
    if defect == "day":
        step["removal_requests"][0]["day"] = 3
    elif defect == "missing_request":
        step["removal_requests"] = []
    elif defect == "missing_output":
        step["trimmed_periods"] = []
    elif defect == "overlap_ids":
        step["trimmed_periods"][0]["removed_place_ids"] = ["eval-edit-museum-4"]
    elif defect == "unknown_removed_id":
        step["trimmed_periods"][0]["removed_place_ids"] = ["unknown"]
    else:
        data["cases"][4]["preserved_visits"].append(
            {"day": 2, "period": "afternoon", "place_ids": ["eval-edit-museum-4"]}
        )
    with pytest.raises(ValidationError):
        evaluation.EditEvaluationSuite.model_validate(data)
