"""Переносы сохраняют посещения, действия и обязательные места без полной генерации."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.trip import RestPeriod, TripEditAnalysis
from app.services import ai
from app.services.ai_diagnostics import analysis_validation_details
from app.services.trip_editing import moved_visit_references
from scripts import evaluate_routes as evaluation


def move_analysis(**updates):
    return TripEditAnalysis.model_validate(
        {
            "supported": True,
            "interests_changed": False,
            "interests": None,
            "must_visit_places_changed": False,
            "must_visit_places": ["Art Aquarium Museum GINZA", "Тестовый музей 4"],
            "rest_period_changes": [],
            "period_actions": [],
            "requires_regeneration": False,
            "regeneration_periods": [],
            "place_replacements": [],
            "place_additions": [],
            "place_removals": [],
            "place_moves": [
                {
                    "place_name": "Тестовый музей 4",
                    "source_day": 2,
                    "source_period": "afternoon",
                    "target_day": 1,
                    "target_period": "afternoon",
                }
            ],
            **updates,
        }
    )


@pytest.fixture
def move_case():
    suite = evaluation.load_edit_suite()
    case = suite.cases[5]
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
async def test_move_keeps_required_visit_focus_neighbors_and_uses_no_generation(
    move_case, monkeypatch
):
    case, plan, context = move_case
    parsed = move_analysis()
    validate(case, plan, parsed)
    before = [item.model_dump_json() for item in (case, plan, context, parsed)]
    monkeypatch.setattr(ai, "get_settings", lambda: pytest.fail("Генерация не нужна"))
    generator = AsyncMock(side_effect=AssertionError("Генерация не нужна"))
    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    result = await generate(case, plan, context, parsed)
    assert result.days[1].afternoon == ["Свободное время."]
    assert len(result.days[0].afternoon) == 2
    assert (
        result.days[0]
        .afternoon[0]
        .startswith("Mitsuo Aida Museum: осмотреть архитектурный объект.")
    )
    assert result.days[0].afternoon[1].startswith("Тестовый музей 4: посетить музей.")
    for original, updated in zip(plan.days, result.days, strict=True):
        assert updated.morning[0].startswith(original.morning[0])
        assert updated.evening == original.evening
    assert all(
        evaluation.assess_edit_periods(
            result, case, case.steps[0].expectations, context
        ).values()
    )
    assert (
        evaluation.merge_edit_preferences(case.preferences, parsed) == case.preferences
    )
    assert [item.model_dump_json() for item in (case, plan, context, parsed)] == before
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_back_fills_free_period_without_duplicating_visit(move_case):
    case, plan, context = move_case
    moved = await generate(case, plan, context, move_analysis())
    parsed = move_analysis(
        place_moves=[
            {
                "place_name": "Тестовый музей 4",
                "source_day": 1,
                "source_period": "afternoon",
                "target_day": 2,
                "target_period": "afternoon",
            }
        ]
    )
    result = await generate(case, moved, context, parsed)
    assert len(result.days[0].afternoon) == len(result.days[1].afternoon) == 1
    assert (
        result.days[0]
        .afternoon[0]
        .startswith("Mitsuo Aida Museum: осмотреть архитектурный объект.")
    )
    assert result.days[1].afternoon[0].startswith("Тестовый музей 4: посетить музей.")
    assert "Свободное время" not in str(result.model_dump())


@pytest.mark.asyncio
async def test_move_from_two_visits_keeps_neighbor_and_order(move_case):
    case, plan, context = move_case
    plan.days[1].afternoon.append("Тестовый музей 5: посетить музей.")
    result = await generate(case, plan, context, move_analysis())
    assert len(result.days[1].afternoon) == 1
    assert result.days[1].afternoon[0].startswith("Тестовый музей 5: посетить музей.")


@pytest.mark.asyncio
async def test_two_moves_swap_visits_atomically_preserving_their_focus(move_case):
    case, plan, context = move_case
    parsed = move_analysis(
        place_moves=[
            {
                "place_name": "Тестовый музей 4",
                "source_day": 2,
                "source_period": "afternoon",
                "target_day": 1,
                "target_period": "afternoon",
            },
            {
                "place_name": "Mitsuo Aida Museum",
                "source_day": 1,
                "source_period": "afternoon",
                "target_day": 2,
                "target_period": "afternoon",
            },
        ]
    )
    validate(
        case,
        plan,
        parsed,
        "Перенеси Тестовый музей 4 в первый день днём, Mitsuo Aida Museum — во второй день днём.",
    )
    result = await generate(case, plan, context, parsed)
    assert len(result.days[0].afternoon) == len(result.days[1].afternoon) == 1
    assert result.days[0].afternoon[0].startswith("Тестовый музей 4: посетить музей.")
    assert (
        result.days[1]
        .afternoon[0]
        .startswith("Mitsuo Aida Museum: осмотреть архитектурный объект.")
    )


@pytest.mark.asyncio
async def test_explicit_moves_can_free_all_visits_of_one_day(move_case):
    case, plan, context = move_case
    parsed = move_analysis(
        place_moves=[
            {
                "place_name": "Art Aquarium Museum GINZA",
                "source_day": 1,
                "source_period": "morning",
                "target_day": 2,
                "target_period": "morning",
            },
            {
                "place_name": "Mitsuo Aida Museum",
                "source_day": 1,
                "source_period": "afternoon",
                "target_day": 2,
                "target_period": "afternoon",
            },
        ]
    )
    result = await generate(case, plan, context, parsed)
    assert result.days[0].morning == result.days[0].afternoon == ["Свободное время."]
    assert result.days[0].evening == plan.days[0].evening
    assert len(result.days[1].morning) == len(result.days[1].afternoon) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "full",
        "closed_target",
        "missing_moved",
        "ambiguous_moved",
        "wrong_category",
        "far_moved",
        "far_neighbor",
        "missing_neighbor",
        "rest_target",
        "rest_source",
        "evening_unknown",
    ],
)
async def test_move_failure_is_atomic_and_does_not_generate(
    move_case, monkeypatch, defect
):
    case, plan, context = move_case
    parsed = move_analysis()
    moved = next(p for p in context.places if p.source_place_id == "eval-edit-museum-4")
    neighbor = next(p for p in context.places if p.source_place_id == "eval-edit-aida")
    if defect == "full":
        plan.days[0].afternoon.append("Неспешная пауза.")
    elif defect == "closed_target":
        moved.opening_hours = "Mo-Su 19:00-23:00"
    elif defect == "missing_moved":
        context.places.remove(moved)
    elif defect == "ambiguous_moved":
        context.places.append(moved.model_copy(update={"source_place_id": "duplicate"}))
    elif defect == "wrong_category":
        moved.categories = ["leisure.park"]
    elif defect == "far_moved":
        moved.latitude = 35.8
    elif defect == "far_neighbor":
        neighbor.latitude = 35.8
    elif defect == "missing_neighbor":
        context.places.remove(neighbor)
    elif defect in {"rest_target", "rest_source"}:
        number = 1 if defect == "rest_target" else 2
        case.preferences.rest_periods = [RestPeriod(day=number, period="afternoon")]
        if number == 1:
            plan.days[0].afternoon = ["Спокойный отдых."]
    else:
        parsed.place_moves[0].target_period = "evening"
        moved.opening_hours = None
    before = [item.model_dump_json() for item in (case, plan, context, parsed)]
    generator = AsyncMock(side_effect=AssertionError("Отказ до генерации"))
    monkeypatch.setattr(ai, "_generate_grounded_plan", generator)
    with pytest.raises(ai.AIServiceError, match="Маршрут не изменён"):
        await generate(case, plan, context, parsed)
    assert [item.model_dump_json() for item in (case, plan, context, parsed)] == before
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_moved_place_is_checked_at_target_time_not_old_time(move_case):
    case, plan, context = move_case
    moved = next(p for p in context.places if p.source_place_id == "eval-edit-museum-4")
    moved.opening_hours = "Mo-Su 09:00-11:00"
    parsed = move_analysis()
    parsed.place_moves[0].target_period = "morning"
    result = await generate(case, plan, context, parsed)
    assert result.days[1].afternoon == ["Свободное время."]
    assert result.days[0].morning[1].startswith("Тестовый музей 4: посетить музей.")


@pytest.mark.asyncio
async def test_saved_metadata_is_replaced_with_fresh_data_during_move(move_case):
    case, plan, context = move_case
    plan.days[1].afternoon[0] += (
        " Часы по данным Geoapify: OLD HOURS. Сайт из данных Geoapify: https://old.invalid/"
    )
    result = await generate(case, plan, context, move_analysis())
    assert "OLD HOURS" not in str(result.model_dump())
    assert "old.invalid" not in str(result.model_dump())
    assert "09:00–17:00" in result.days[0].afternoon[1]


@pytest.mark.parametrize(
    ("defect", "reason"),
    [
        ("source_day", "move_day_outside_trip"),
        ("target_day", "move_day_outside_trip"),
        ("unknown", "move_reference_not_unique"),
        ("wrong_source", "move_reference_not_unique"),
        ("not_explicit", "move_not_explicit"),
        ("duplicate_alias", "duplicate_move_reference"),
        ("ambiguous", "move_reference_not_unique"),
        ("generation_source", "move_scope_conflict"),
        ("generation_target", "move_scope_conflict"),
        ("rest_change", "move_scope_conflict"),
        ("walk", "move_scope_conflict"),
        ("addition", "move_scope_conflict"),
        ("removal", "move_scope_conflict"),
        ("no_plan", "move_plan_unavailable"),
    ],
)
def test_analysis_rejects_invalid_move_identity_scope_and_implicit_name(
    move_case, defect, reason
):
    case, plan, _ = move_case
    parsed = move_analysis()
    instruction = case.steps[0].user_message
    if defect in {"source_day", "target_day"}:
        setattr(parsed.place_moves[0], defect, 3)
    elif defect == "unknown":
        parsed.place_moves[0].place_name = "Неизвестный музей"
    elif defect == "wrong_source":
        parsed.place_moves[0].source_period = "morning"
    elif defect == "not_explicit":
        instruction = "Все активности оставь."
    elif defect == "duplicate_alias":
        duplicate = parsed.place_moves[0].model_copy(
            update={"place_name": "ТЕСТОВЫЙ МУЗЕЙ 4!", "target_period": "morning"}
        )
        parsed.place_moves.append(duplicate)
    elif defect == "ambiguous":
        plan.days[1].afternoon *= 2
    elif defect.startswith("generation"):
        day = 2 if defect.endswith("source") else 1
        parsed.requires_regeneration = True
        parsed.regeneration_periods = [
            evaluation.TripPeriodScope(day=day, period="afternoon")
        ]
    elif defect == "rest_change":
        parsed = move_analysis(
            rest_period_changes=[{"day": None, "period": "afternoon", "rest": False}]
        )
    elif defect == "walk":
        parsed = move_analysis(
            period_actions=[{"day": 1, "period": "afternoon", "action": "quiet_walk"}]
        )
    elif defect == "addition":
        parsed = move_analysis(
            place_additions=[
                {
                    "day": 1,
                    "period": "afternoon",
                    "place_name": "Тестовый музей 5",
                    "activity_focus": None,
                }
            ]
        )
    elif defect == "removal":
        parsed = move_analysis(
            place_removals=[
                {"day": 2, "period": "afternoon", "place_name": "Тестовый музей 4"}
            ]
        )
    else:
        plan = None
    with pytest.raises(ValueError) as caught:
        validate(case, plan, parsed, instruction)
    diagnostic = analysis_validation_details(caught.value, stage="semantic")
    assert diagnostic["validation_reason"] == reason
    assert "музей" not in str(diagnostic)


@pytest.mark.parametrize(
    ("defect", "reason"),
    [
        ("missing", "schema_validation"),
        ("same_period", "move_same_period"),
        ("duplicate", "duplicate_moves"),
        ("null_day", "schema_validation"),
        ("str_day", "schema_validation"),
    ],
)
def test_move_schema_is_required_strict_and_has_safe_diagnostics(defect, reason):
    data = move_analysis().model_dump()
    if defect == "missing":
        del data["place_moves"]
    elif defect == "same_period":
        data["place_moves"][0]["target_day"] = 2
    elif defect == "duplicate":
        data["place_moves"].append(deepcopy(data["place_moves"][0]))
    else:
        data["place_moves"][0]["target_day"] = None if defect == "null_day" else "1"
    with pytest.raises(ValidationError) as caught:
        TripEditAnalysis.model_validate(data)
    assert (
        analysis_validation_details(caught.value, stage="schema")["validation_reason"]
        == reason
    )


@pytest.mark.asyncio
async def test_preserve_period_actions_keep_move_neighbors(move_case):
    case, plan, context = move_case
    parsed = move_analysis(
        period_actions=[{"day": None, "period": "afternoon", "action": "preserve"}]
    )
    result = await generate(case, plan, context, parsed)
    assert len(result.days[0].afternoon) == 2
    assert result.days[1].afternoon == ["Свободное время."]


@pytest.mark.asyncio
async def test_one_day_move_can_free_afternoon_without_automatic_replacement(move_case):
    case, plan, context = move_case
    case.preferences = case.preferences.model_copy(
        update={"duration_days": 1, "must_visit_places": ["Art Aquarium Museum GINZA"]}
    )
    plan = plan.model_copy(
        deep=True, update={"duration_days": 1, "days": plan.days[:1]}
    )
    parsed = move_analysis(
        must_visit_places=case.preferences.must_visit_places,
        place_moves=[
            {
                "place_name": "Mitsuo Aida Museum",
                "source_day": 1,
                "source_period": "afternoon",
                "target_day": 1,
                "target_period": "morning",
            }
        ],
    )
    result = await generate(case, plan, context, parsed)
    assert result.days[0].afternoon == ["Свободное время."]
    assert (
        result.days[0]
        .morning[1]
        .startswith("Mitsuo Aida Museum: осмотреть архитектурный объект.")
    )


@pytest.mark.asyncio
async def test_followup_preserve_and_removal_accept_saved_sparse_day(move_case):
    case, plan, context = move_case
    moved = await generate(case, plan, context, move_analysis())
    unchanged = await generate(case, moved, context, move_analysis(place_moves=[]))
    assert unchanged.days == moved.days
    removed = await generate(
        case,
        unchanged,
        context,
        move_analysis(
            place_moves=[],
            must_visit_places_changed=True,
            must_visit_places=["Art Aquarium Museum GINZA"],
            place_removals=[
                {"day": 1, "period": "afternoon", "place_name": "Тестовый музей 4"}
            ],
        ),
    )
    assert len(removed.days[0].afternoon) == 1
    assert removed.days[1].afternoon == ["Свободное время."]
    assert removed.days[1].morning == moved.days[1].morning


@pytest.mark.asyncio
async def test_same_sparse_plan_without_local_locks_still_fails_generation_minimum(
    move_case,
):
    from app.services.trip_editing import PERIODS, resolve_preserved_activities

    case, plan, context = move_case
    result = await generate(case, plan, context, move_analysis())
    cells = {(day.day, period) for day in result.days for period in PERIODS}
    activities = resolve_preserved_activities(result, cells, context)
    raw = {
        "destination": "Токио",
        "duration_days": 2,
        "summary": "План поездки",
        "days": [
            {
                "day": day.day,
                "title": "План дня",
                **{
                    period: [item.model_dump() for item in activities[day.day, period]]
                    for period in PERIODS
                },
            }
            for day in result.days
        ],
    }
    import json

    with pytest.raises(ValueError, match="too few grounded places"):
        ai._validate_grounded_trip_plan(
            json.dumps(raw),
            preferences=case.preferences,
            travel_context=context,
            shorten_afternoons=False,
        )


@pytest.mark.asyncio
async def test_alias_move_keeps_old_focus_from_saved_visit(move_case):
    case, plan, context = move_case
    plan.days[1].afternoon = [
        "Тестовый музей 4 (Archive Four): осмотреть архитектурный объект."
    ]
    place = next(p for p in context.places if p.source_place_id == "eval-edit-museum-4")
    place.categories.append("building.tourism")
    parsed = move_analysis()
    references = moved_visit_references(plan, parsed)
    assert references[0][1:] == ("Тестовый музей 4 (Archive Four)", "architecture")
    result = await generate(case, plan, context, parsed)
    assert (
        result.days[0]
        .afternoon[1]
        .startswith("Тестовый музей 4: осмотреть архитектурный объект.")
    )


@pytest.mark.parametrize("first_invalid", [False, True])
def test_real_analysis_http_and_local_move_pass_live_eval_report(
    move_case, monkeypatch, tmp_path, first_invalid
):
    import json
    from types import SimpleNamespace

    import httpx

    case, _, _ = move_case
    valid = move_analysis().model_dump()
    replies = [valid]
    if first_invalid:
        invalid = deepcopy(valid)
        del invalid["place_moves"][0]["target_period"]
        replies.insert(0, invalid)
    calls = []

    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "fixture-analysis"
        assert payload["response_format"]["json_schema"]["name"] == "trip_edit_analysis"
        assert (
            "place_moves"
            in payload["response_format"]["json_schema"]["schema"]["required"]
        )
        incoming = json.loads(payload["messages"][1]["content"])
        assert incoming["edit_instruction"] == case.steps[0].user_message
        assert (
            "expectations" not in incoming
            and "minimum_places_per_day_by_day" not in incoming
        )
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
    path = tmp_path / "move.json"
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
            str(path),
        ]
    )
    report = json.loads(path.read_text())
    assert code == 0 and report["summary"] == {"passed": 1}
    row = report["results"][0]
    assert all(row["checks"].values())
    assert len(calls) == 1 + first_invalid
    assert (
        row["steps"][0]["updated_preferences"] == row["steps"][0]["input_preferences"]
    )
    if first_invalid:
        assert (
            row["calls"][0]["validation_issues"][0]["field"]
            == "place_moves[].target_period"
        )
    assert "fixture-secret" not in path.read_text()
    assert not replies


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defect", "failed_check"),
    [
        ("duplicate", "move_unique:eval-edit-museum-4"),
        ("missing_target", "move_target:1:afternoon"),
        ("old_focus_changed", "move_target_focus:1:afternoon"),
        ("moved_focus_changed", "move_target_focus:1:afternoon"),
        ("rest_instead_of_free", "move_source_focus:2:afternoon"),
    ],
)
async def test_independent_move_eval_rejects_wrong_positions_duplicates_and_actions(
    move_case, defect, failed_check
):
    case, plan, context = move_case
    result = await generate(case, plan, context, move_analysis())
    if defect == "duplicate":
        result.days[1].afternoon = plan.days[1].afternoon
    elif defect == "missing_target":
        result.days[0].afternoon = result.days[0].afternoon[:1]
        result.summary += " Тестовый музей 4."
    elif defect == "old_focus_changed":
        result.days[0].afternoon[0] = "Mitsuo Aida Museum: посетить музей."
    elif defect == "moved_focus_changed":
        result.days[0].afternoon[1] = (
            "Тестовый музей 4: осмотреть архитектурный объект."
        )
    else:
        result.days[1].afternoon = ["Спокойный отдых."]
    checks = evaluation.assess_edit_periods(
        result, case, case.steps[0].expectations, context
    )
    assert not checks[failed_check]


@pytest.mark.parametrize(
    "defect",
    [
        "day",
        "overlap_scope",
        "unknown_id",
        "focus_count",
        "moved_id_absent",
        "missing_minimum_day",
        "negative_minimum",
    ],
)
def test_move_eval_rejects_inconsistent_golden_criteria(defect):
    data = evaluation.load_edit_suite().model_dump(mode="json")
    case = data["cases"][5]
    expected = case["steps"][0]["expectations"]
    visit = expected["moved_visits"][0]
    if defect == "day":
        visit["target_day"] = 3
    elif defect == "overlap_scope":
        case["preserved_visits"].append(
            {"day": 2, "period": "afternoon", "place_ids": ["eval-edit-museum-4"]}
        )
    elif defect == "unknown_id":
        visit["moved_place_id"] = "unknown"
        visit["target_place_ids"][1] = "unknown"
    elif defect == "focus_count":
        visit["target_activity_focuses"] = ["museum"]
    elif defect == "moved_id_absent":
        visit["moved_place_id"] = "eval-edit-museum-5"
    elif defect == "missing_minimum_day":
        del expected["minimum_places_per_day_by_day"]["2"]
    else:
        expected["minimum_places_per_day_by_day"]["2"] = -1
    with pytest.raises(ValidationError):
        evaluation.EditEvaluationSuite.model_validate(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_first", [None, "far", "duplicate"])
async def test_move_constraints_survive_generation_and_retry_in_another_period(
    move_case, monkeypatch, bad_first
):
    """Генерация другого периода не возвращает перенесённый ID и не растягивает целевой день."""

    import json
    from types import SimpleNamespace

    import httpx

    case, plan, context = move_case
    far = next(p for p in context.places if p.source_place_id == "eval-edit-far-museum")
    far.opening_hours = "24/7"
    moved = next(p for p in context.places if p.source_place_id == "eval-edit-museum-4")
    moved.opening_hours = "24/7"
    general = {
        "source_place_id": None,
        "place_name": None,
        "activity_focus": None,
        "description": "Спокойная прогулка.",
    }
    valid = {
        "destination": "Токио",
        "duration_days": 2,
        "summary": "План поездки",
        "days": [
            {
                "day": day,
                "title": "План дня",
                **{period: [deepcopy(general)] for period in evaluation.PERIODS},
            }
            for day in (1, 2)
        ],
    }
    replies = [valid]
    if bad_first:
        bad = deepcopy(valid)
        candidate = far if bad_first == "far" else moved
        bad["days"][0]["evening"] = [
            {
                "source_place_id": candidate.source_place_id,
                "place_name": candidate.name,
                "activity_focus": "museum",
                "description": None,
            }
        ]
        replies.insert(0, bad)
    calls = []

    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "fixture-generation"
        incoming = json.loads(payload["messages"][1]["content"])
        assert incoming["place_moves"] == [
            {
                "target_day": 1,
                "target_period": "afternoon",
                "source_place_id": "eval-edit-museum-4",
            }
        ]
        assert (
            incoming["travel_context"]["grounding_requirements"][
                "minimum_places_per_day_by_day"
            ]["2"]
            == 1
        )
        return httpx.Response(
            200,
            json={
                "model": "fixture-generation",
                "choices": [
                    {
                        "message": {"content": json.dumps(replies.pop(0))},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
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
    parsed = move_analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": 1, "period": "evening"}],
    )
    result = await generate(case, plan, context, parsed)
    assert result.days[1].afternoon == ["Свободное время."]
    assert len(result.days[0].afternoon) == 2
    assert (
        result.days[0]
        .afternoon[0]
        .startswith("Mitsuo Aida Museum: осмотреть архитектурный объект.")
    )
    assert result.days[0].afternoon[1].startswith("Тестовый музей 4: посетить музей.")
    assert result.days[0].evening == ["Спокойная прогулка."]
    assert len(calls) == (2 if bad_first else 1)
    assert not replies


@pytest.mark.asyncio
async def test_evening_park_move_and_followup_preserve_keep_free_evening(move_case):
    case, plan, context = move_case
    case.preferences = case.preferences.model_copy(
        update={
            "duration_days": 1,
            "interests": "Парки",
            "must_visit_places": ["Art Aquarium Museum GINZA"],
        }
    )
    plan = plan.model_copy(
        deep=True, update={"duration_days": 1, "days": plan.days[:1]}
    )
    park = next(p for p in context.places if p.source_place_id == "eval-edit-aida")
    park.categories = ["leisure.park"]
    park.opening_hours = "24/7"
    evening = next(
        p for p in context.places if p.source_place_id == "eval-edit-near-park"
    )
    evening.opening_hours = "24/7"
    plan.days[0].afternoon = [f"{park.display_name}: прогуляться по парку."]
    plan.days[0].evening = [f"{evening.display_name}: прогуляться по парку."]
    parsed = move_analysis(
        must_visit_places=case.preferences.must_visit_places,
        place_moves=[
            {
                "place_name": evening.display_name,
                "source_day": 1,
                "source_period": "evening",
                "target_day": 1,
                "target_period": "afternoon",
            }
        ],
    )
    result = await generate(case, plan, context, parsed)
    assert result.days[0].evening == ["Свободное время."]
    assert len(result.days[0].afternoon) == 2
    unchanged = await generate(
        case,
        result,
        context,
        move_analysis(
            must_visit_places=case.preferences.must_visit_places, place_moves=[]
        ),
    )
    assert unchanged.days == result.days
