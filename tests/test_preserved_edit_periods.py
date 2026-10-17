"""Сохранение посещений и локальные правки через настоящие AI-функции."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.trip import TripEditAnalysis, TripPreferences
from app.services import ai
from app.services.trip_editing import (
    period_action_map,
    preserved_cells,
    regeneration_cells,
    saved_visit_reference,
)
from scripts import evaluate_routes as evaluation


def analysis(**updates: Any) -> TripEditAnalysis:
    return TripEditAnalysis.model_validate(
        {
            "supported": True,
            "interests_changed": False,
            "interests": None,
            "must_visit_places_changed": False,
            "must_visit_places": ["Art Aquarium Museum GINZA"],
            "rest_period_changes": [],
            "period_actions": [],
            "place_replacements": [],
            "requires_regeneration": False,
            "regeneration_periods": [],
            **updates,
        }
    )


def install_provider(monkeypatch, response):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        content = response(payload)
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

    original = httpx.AsyncClient
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="PRIVATE_KEY",
        llm_model="generation",
        llm_analysis_model="analysis",
    )
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("case_index", [0, 1])
async def test_local_edit_cycle_preserves_actual_state_with_only_analysis_calls(
    monkeypatch, case_index
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[case_index]
    before = suite.model_dump_json()
    turn = 0

    def response(payload):
        nonlocal turn
        assert (
            payload["response_format"]["json_schema"]["name"] == "trip_edit_analysis"
        ), "Генерация не нужна для локальной правки."
        step = case.steps[turn]
        turn += 1
        actions = []
        if turn == 3:
            actions = [
                {
                    "day": 2 if case_index else None,
                    "period": "evening",
                    "action": "quiet_walk",
                }
            ]
        elif turn == 1 and case_index:
            actions = [{"day": 1, "period": "evening", "action": "quiet_walk"}]
        return analysis(
            rest_period_changes=[
                rule.model_dump() for rule in step.expectations.rest_period_changes
            ],
            period_actions=actions,
        ).model_dump()

    requests = install_provider(monkeypatch, response)
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed", result
    assert len(requests) == len(result["calls"]) == 3
    assert not result["retry_used"]
    assert result["known_total_tokens"] == 90
    assert all(result["checks"].values())
    for old, new in zip(result["steps"][:-1], result["steps"][1:], strict=True):
        assert new["input_plan"] == old["plan"]
        assert new["input_preferences"] == old["updated_preferences"]
    assert suite.model_dump_json() == before


@pytest.mark.asyncio
async def test_local_edit_uses_fresh_metadata_without_generation(monkeypatch):
    suite = evaluation.load_edit_suite()
    case = suite.cases[1]
    saved = suite.plans[case.current_plan].model_copy(deep=True)
    saved.days[1].afternoon[0] += (
        " Часы по данным Geoapify: старое расписание."
        " Сайт из данных Geoapify: https://example.invalid/old-site"
    )
    context = suite.contexts[case.context].model_copy(deep=True)
    context.places[-1].opening_hours = "Mo-Su 11:00-16:00"
    context.places[-1].website = "https://example.invalid/new-site"
    context.places[-1].available_details = []

    def no_settings():
        pytest.fail("Генерация и её настройки не нужны.")

    monkeypatch.setattr(ai, "get_settings", no_settings)
    result = await ai.generate_edited_trip_plan(
        current_preferences=case.preferences,
        preferences=case.preferences,
        current_plan=saved,
        travel_context=context,
        instruction="Музеи оставь.",
        analysis=analysis(),
    )
    assert "11:00–16:00" in result.days[1].afternoon[0]
    assert "new-site" in result.days[1].afternoon[0]
    assert "old-site" not in result.model_dump_json()
    assert result.days[0].evening == saved.days[0].evening
    assert result.days[1].afternoon[0].startswith("Тестовый музей 4:")


@pytest.mark.asyncio
async def test_local_edit_protects_sparse_saved_visit_from_candidate_filtering(
    monkeypatch,
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[1]
    context = suite.contexts[case.context].model_copy(deep=True)
    context.places[-1].opening_hours = None
    context.places.append(
        context.places[0].model_copy(
            update={"name": "Другой музей", "source_place_id": "replacement"}
        )
    )

    def no_settings():
        pytest.fail("Локальная правка не требует генерации.")

    monkeypatch.setattr(ai, "get_settings", no_settings)
    result = await ai.generate_edited_trip_plan(
        current_preferences=case.preferences,
        preferences=case.preferences,
        current_plan=suite.plans[case.current_plan],
        travel_context=context,
        instruction="Музеи оставь.",
        analysis=analysis(),
    )
    assert result.days[1].afternoon[0] == "Тестовый музей 4: посетить музей."


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing", "ambiguous", "closed", "category"])
@pytest.mark.parametrize("regenerate", [False, True])
async def test_unverifiable_saved_visit_fails_before_generation(
    monkeypatch, defect, regenerate
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[1]
    saved = suite.plans[case.current_plan]
    before = saved.model_dump_json()
    context = suite.contexts[case.context].model_copy(deep=True)
    if defect == "missing":
        context.places.pop()
    elif defect == "ambiguous":
        context.places.append(
            context.places[-1].model_copy(update={"source_place_id": "other-id"})
        )
    elif defect == "closed":
        context.places[-1].opening_hours = "off"
    else:
        context.places[-1].categories = ["leisure.park"]

    def no_settings():
        pytest.fail("Не следует тратить генерацию на неподтверждённое сохранение.")

    monkeypatch.setattr(ai, "get_settings", no_settings)
    with pytest.raises(ai.AIServiceError, match="Маршрут не изменён"):
        await ai.generate_edited_trip_plan(
            current_preferences=case.preferences,
            preferences=case.preferences,
            current_plan=saved,
            travel_context=context,
            instruction="Остальное оставь.",
            analysis=analysis(
                requires_regeneration=regenerate,
                regeneration_periods=[{"day": 2, "period": "evening"}]
                if regenerate
                else [],
            ),
        )
    assert saved.model_dump_json() == before


@pytest.mark.asyncio
async def test_full_generation_cannot_replace_explicitly_preserved_museum(monkeypatch):
    suite = evaluation.load_edit_suite()
    case = suite.cases[1]
    context = suite.contexts[case.context].model_copy(deep=True)
    replacement = context.places[-1].model_copy(
        update={"source_place_id": "police-id", "name": "Police Museum"}
    )
    context.places.append(replacement)

    def response(payload):
        places = context.places

        def visit(index):
            place = places[index]
            return dict(
                source_place_id=place.source_place_id,
                place_name=place.name,
                activity_focus="museum",
                description=None,
            )

        return dict(
            destination="Токио",
            duration_days=2,
            summary="План поездки",
            days=[
                dict(
                    day=day,
                    title="План дня",
                    morning=[visit((day - 1) * 2)],
                    afternoon=[visit(4 if day == 2 else 1)],
                    evening=[
                        dict(
                            source_place_id=None,
                            place_name=None,
                            activity_focus=None,
                            description="Спокойная прогулка.",
                        )
                    ],
                )
                for day in (1, 2)
            ],
        )

    requests = install_provider(monkeypatch, response)
    result = await ai.generate_edited_trip_plan(
        current_preferences=case.preferences,
        preferences=case.preferences,
        current_plan=suite.plans[case.current_plan],
        travel_context=context,
        instruction="Музеи утром и днём оставь. Вечер поменяй.",
        analysis=analysis(
            requires_regeneration=True,
            regeneration_periods=[
                {"day": None, "period": period}
                for period in ("morning", "afternoon", "evening")
            ],
            period_actions=[
                {"day": None, "period": period, "action": "preserve"}
                for period in ("morning", "afternoon")
            ],
        ),
    )
    assert len(requests) == 1
    assert result.days[1].afternoon[0].startswith("Тестовый музей 4:")
    assert "Police Museum" not in result.model_dump_json()
    assert (
        len(json.loads(requests[0]["messages"][1]["content"])["preserved_periods"]) == 4
    )


def test_specific_walk_overrides_general_preservation():
    parsed = analysis(
        period_actions=[
            {"day": 2, "period": "evening", "action": "quiet_walk"},
            {"day": None, "period": "evening", "action": "preserve"},
        ]
    )
    prefs = TripPreferences(destination="Токио", duration_days=2)
    actions = period_action_map(parsed, 2)
    assert actions[1, "evening"] == "preserve"
    assert actions[2, "evening"] == "quiet_walk"
    assert (2, "evening") not in preserved_cells(parsed, prefs, prefs)


@pytest.mark.parametrize("defect", ["duplicate", "wrong_day", "changed_preferences"])
def test_invalid_period_analysis_is_rejected(defect):
    with pytest.raises((ValidationError, ValueError)):
        parsed = analysis(
            interests_changed=defect == "changed_preferences",
            period_actions=[
                {
                    "day": 3 if defect == "wrong_day" else 1,
                    "period": "morning",
                    "action": "preserve",
                }
            ]
            * (2 if defect == "duplicate" else 1),
        )
        period_action_map(parsed, 2)


def test_saved_reference_keeps_colons_in_name_and_discards_only_provider_suffix():
    assert saved_visit_reference(
        "Музей: главный корпус: посетить музей. Часы по данным Google Maps: старые часы."
    ) == ("Музей: главный корпус", "museum")
    assert saved_visit_reference("Спокойная прогулка.") is None
    assert saved_visit_reference("Музей: посетить музей. Потом другое занятие.") is None


@pytest.mark.parametrize("duration", [2, 30])
def test_regeneration_preserves_complement_without_explicit_preserve_actions(duration):
    parsed = analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": 2, "period": "afternoon"}],
    )
    prefs = TripPreferences(destination="Токио", duration_days=duration)
    cells = preserved_cells(parsed, prefs, prefs)
    assert len(cells) == duration * 3 - 1
    assert (2, "afternoon") not in cells
    assert {(1, "afternoon"), (2, "morning"), (2, "evening")} <= cells


def test_general_generation_scope_can_have_explicit_preserved_exception():
    parsed = analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": None, "period": "afternoon"}],
        period_actions=[{"day": 1, "period": "afternoon", "action": "preserve"}],
    )
    prefs = TripPreferences(destination="Токио", duration_days=2)
    assert regeneration_cells(parsed, 2) == {(1, "afternoon"), (2, "afternoon")}
    assert len(preserved_cells(parsed, prefs, prefs)) == 5


@pytest.mark.parametrize(
    "defect", ["missing", "empty", "local", "duplicate", "wrong_day"]
)
def test_regeneration_scope_must_be_explicit_and_consistent(defect):
    data = analysis().model_dump()
    data.update(
        requires_regeneration=defect != "local",
        regeneration_periods=[
            {"day": 3 if defect == "wrong_day" else 2, "period": "afternoon"}
        ],
    )
    if defect == "missing":
        data.pop("regeneration_periods")
    elif defect == "empty":
        data["regeneration_periods"] = []
    elif defect == "duplicate":
        data["regeneration_periods"] *= 2
    with pytest.raises((ValidationError, ValueError)):
        parsed = TripEditAnalysis.model_validate(data)
        ai._validate_trip_edit_analysis(
            analysis=parsed,
            current_preferences=TripPreferences(destination="Токио", duration_days=2),
            instruction="Во второй день днём замени музей. Остальное оставь.",
        )


@pytest.mark.asyncio
async def test_empty_regeneration_scope_uses_existing_bounded_analysis_retry(
    monkeypatch,
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    valid = analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": 2, "period": "afternoon"}],
    ).model_dump()
    count = 0

    def response(payload):
        nonlocal count
        count += 1
        assert payload["response_format"]["json_schema"]["name"] == "trip_edit_analysis"
        return {**valid, "regeneration_periods": []} if count == 1 else valid

    requests = install_provider(monkeypatch, response)
    parsed = await ai.analyze_trip_edit(
        current_preferences=case.preferences,
        current_plan=suite.plans[case.current_plan],
        instruction=case.steps[0].user_message,
    )
    assert len(requests) == 2
    assert regeneration_cells(parsed, 2) == {(2, "afternoon")}


def replacement_analysis(*, nearby=True, activity_focus="museum", **updates):
    return analysis(
        requires_regeneration=True,
        regeneration_periods=[{"day": 2, "period": "afternoon"}],
        place_replacements=[
            {
                "day": 2,
                "period": "afternoon",
                "place_name": "Тестовый музей 4",
                "activity_focus": activity_focus,
                "nearby": nearby,
            }
        ],
        **updates,
    )


def replacement_plan(context, target_id, first_afternoon_id="eval-edit-aida"):
    by_id = {place.source_place_id: place for place in context.places}

    def visit(place_id):
        place = by_id[place_id]
        return dict(
            source_place_id=place_id,
            place_name=place.name,
            activity_focus="museum",
            description=None,
        )

    def general():
        return dict(
            source_place_id=None,
            place_name=None,
            activity_focus=None,
            description="Другая прогулка.",
        )

    return dict(
        destination="Токио",
        duration_days=2,
        summary="План поездки",
        days=[
            dict(
                day=1,
                title="План дня",
                morning=[visit("eval-edit-aquarium")],
                afternoon=[visit(first_afternoon_id)],
                evening=[general()],
            ),
            dict(
                day=2,
                title="План дня",
                morning=[visit("eval-edit-museum-3")],
                afternoon=[visit(target_id)],
                evening=[general()],
            ),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("old_preserved_elsewhere", [False, True])
async def test_replacement_excludes_old_id_and_validates_final_period(
    monkeypatch, old_preserved_elsewhere, caplog
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    context = suite.contexts[case.context]
    saved = suite.plans[case.current_plan].model_copy(deep=True)
    first_afternoon_id = "eval-edit-aida"
    if old_preserved_elsewhere:
        saved.days[0].afternoon = ["Тестовый музей 4: посетить музей."]
        first_afternoon_id = "eval-edit-museum-4"
    before = saved.model_dump_json()
    context_before = context.model_dump_json()
    count = 0

    def response(payload):
        nonlocal count
        count += 1
        ids = payload["response_format"]["json_schema"]["schema"]["$defs"][
            "GroundedActivity"
        ]["properties"]["source_place_id"]["anyOf"][0]["enum"]
        assert ("eval-edit-museum-4" in ids) == old_preserved_elsewhere
        assert {
            "eval-edit-aquarium",
            "eval-edit-museum-3",
            "eval-edit-museum-5",
        } <= set(ids)
        incoming = json.loads(payload["messages"][1]["content"])
        assert incoming["place_replacements"] == [
            {
                "day": 2,
                "period": "afternoon",
                "excluded_source_place_ids": ["eval-edit-museum-4"],
                "previous_source_place_ids": ["eval-edit-museum-4"],
                "minimum_new_places": 1,
                "visit_requirements": [
                    {
                        "activity_focus": "museum",
                        "nearby": True,
                        "maximum_distance_meters": 2000,
                        "allowed_source_place_ids": (
                            ["eval-edit-aida", "eval-edit-museum-5"]
                            if old_preserved_elsewhere
                            else ["eval-edit-museum-5"]
                        ),
                    }
                ],
            }
        ]
        if count == 2:
            assert "place_replacements" in payload["messages"][-1]["content"]
        # Имитируем игнорирование схемы провайдером на первой попытке.
        return replacement_plan(
            context,
            "eval-edit-museum-4" if count == 1 else "eval-edit-museum-5",
            first_afternoon_id,
        )

    requests = install_provider(monkeypatch, response)
    result = await ai.generate_edited_trip_plan(
        preferences=case.preferences,
        current_preferences=case.preferences,
        travel_context=context,
        current_plan=saved,
        instruction=case.steps[0].user_message,
        analysis=replacement_analysis(),
    )
    assert len(requests) == 2
    assert result.days[1].afternoon[0].startswith("Тестовый музей 5: посетить музей.")
    assert result.days[0].afternoon[0].startswith(saved.days[0].afternoon[0])
    assert all(
        new.evening == old.evening
        for new, old in zip(result.days, saved.days, strict=True)
    )
    assert saved.model_dump_json() == before
    assert context.model_dump_json() == context_before
    assert any(
        "unfulfilled_place_replacement" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    ["no_alternative", "closed_alternative", "general_instead", "old_id_twice"],
)
async def test_unfulfilled_replacement_fails_without_mutating_saved_plan(
    monkeypatch, defect
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    context = suite.contexts[case.context].model_copy(deep=True)
    saved = suite.plans[case.current_plan]
    before = saved.model_dump_json()
    if defect == "no_alternative":
        context.places = [
            place
            for place in context.places
            if place.source_place_id != "eval-edit-museum-5"
        ]
    elif defect == "closed_alternative":
        next(
            place
            for place in context.places
            if place.source_place_id == "eval-edit-museum-5"
        ).opening_hours = "off"

    def response(_):
        if defect in {"no_alternative", "closed_alternative"}:
            pytest.fail("Без подтверждённой альтернативы генерация не нужна.")
        result = replacement_plan(context, "eval-edit-museum-4")
        if defect == "general_instead":
            result["days"][1]["afternoon"] = [
                dict(
                    source_place_id=None,
                    place_name=None,
                    activity_focus=None,
                    description="Отдохнуть.",
                )
            ]
        return result

    requests = install_provider(monkeypatch, response)
    with pytest.raises(ai.AIServiceError):
        await ai.generate_edited_trip_plan(
            preferences=case.preferences,
            current_preferences=case.preferences,
            travel_context=context,
            current_plan=saved,
            instruction=case.steps[0].user_message,
            analysis=replacement_analysis(),
        )
    assert len(requests) == (
        0 if defect in {"no_alternative", "closed_alternative"} else 2
    )
    assert saved.model_dump_json() == before


@pytest.mark.asyncio
async def test_replacement_can_proceed_when_old_venue_is_absent_from_fresh_context(
    monkeypatch,
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    context = suite.contexts[case.context].model_copy(deep=True)
    context.places = [
        p for p in context.places if p.source_place_id != "eval-edit-museum-4"
    ]
    requests = install_provider(
        monkeypatch, lambda _: replacement_plan(context, "eval-edit-museum-5")
    )
    result = await ai.generate_edited_trip_plan(
        preferences=case.preferences,
        current_preferences=case.preferences,
        travel_context=context,
        current_plan=suite.plans[case.current_plan],
        instruction=case.steps[0].user_message,
        analysis=replacement_analysis(nearby=False),
    )
    assert len(requests) == 1
    assert result.days[1].afternoon[0].startswith("Тестовый музей 5:")


@pytest.mark.parametrize(
    "defect",
    ["missing_field", "wrong_name", "wrong_scope", "preserve", "duplicate", "local"],
)
def test_invalid_replacement_analysis_is_rejected(defect):
    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    data = replacement_analysis().model_dump()
    if defect == "missing_field":
        data.pop("place_replacements")
    elif defect == "wrong_name":
        data["place_replacements"][0]["place_name"] = "Неназванное в маршруте место"
    elif defect == "wrong_scope":
        data["regeneration_periods"][0]["period"] = "evening"
    elif defect == "preserve":
        data["period_actions"] = [
            {"day": 2, "period": "afternoon", "action": "preserve"}
        ]
    elif defect == "duplicate":
        data["place_replacements"] *= 2
    elif defect == "local":
        data["requires_regeneration"] = False
        data["regeneration_periods"] = []
    with pytest.raises((ValidationError, ValueError)):
        parsed = TripEditAnalysis.model_validate(data)
        ai._validate_trip_edit_analysis(
            analysis=parsed,
            current_preferences=case.preferences,
            current_plan=suite.plans[case.current_plan],
            instruction=case.steps[0].user_message,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_park", [False, True])
@pytest.mark.parametrize(
    ("explicit_replacement", "google_location"),
    [(False, False), (True, False), (True, True)],
)
async def test_one_day_afternoon_replacement_can_override_initial_interest(
    monkeypatch, caplog, extra_park, explicit_replacement, google_location
):
    """Явная замена парка на музей не меняет общий интерес и соседние периоды."""

    place_data = [
        ("required", "Обязательный музей", "entertainment.museum"),
        ("new-museum", "Другой музей", "entertainment.museum"),
        ("day-park", "Дневной парк", "leisure.park"),
        ("evening-park", "Вечерний парк", "leisure.park"),
    ]
    if extra_park:
        place_data.append(("extra-park", "Запасной парк", "leisure.park"))
    context = TravelContext(
        location=DestinationLocation(
            formatted_name="Токио",
            latitude=35.68,
            longitude=139.76,
            source_place_id="test-tokyo",
        ),
        requested_categories=["leisure.park", "entertainment.museum"],
        places=[
            PlaceCandidate(
                source_place_id=place_id,
                name=name,
                formatted_address="Условный тестовый адрес",
                latitude=35.68 + index * 0.001,
                longitude=139.76,
                categories=[category],
                opening_hours="24/7"
                if category == "leisure.park"
                else "Mo-Su 10:00-18:00",
            )
            for index, (place_id, name, category) in enumerate(place_data)
        ],
        fetched_at=datetime(2026, 10, 7, tzinfo=UTC),
    )
    if google_location:
        context.places[1].location_source = "google"
    prefs = TripPreferences(
        destination="Токио",
        duration_days=1,
        interests="Парки",
        must_visit_places=["Обязательный музей"],
    )

    def visit(place_id, name, focus):
        return dict(
            source_place_id=place_id,
            place_name=name,
            activity_focus=focus,
            description=None,
        )

    original = dict(
        destination="Токио",
        duration_days=1,
        summary="План поездки",
        days=[
            dict(
                day=1,
                title="План дня",
                morning=[visit("required", "Обязательный музей", "museum")],
                afternoon=[visit("day-park", "Дневной парк", "park")],
                evening=[visit("evening-park", "Вечерний парк", "park")],
            )
        ],
    )
    saved = ai._validate_grounded_trip_plan(
        json.dumps(original), preferences=prefs, travel_context=context
    )
    before = saved.model_dump_json(), prefs.model_dump_json(), context.model_dump_json()

    def response(payload):
        proposed = json.loads(json.dumps(original))
        # Первая попытка игнорирует обязательную замену, вторая выполняет её.
        if not explicit_replacement or len(requests) == 2:
            proposed["days"][0]["afternoon"] = [
                visit("new-museum", "Другой музей", "museum")
            ]
        return proposed

    requests = install_provider(monkeypatch, response)
    parsed = analysis(
        must_visit_places=prefs.must_visit_places,
        requires_regeneration=True,
        regeneration_periods=[{"day": 1, "period": "afternoon"}],
        place_replacements=[
            {
                "day": 1,
                "period": "afternoon",
                "place_name": "Дневной парк",
                "activity_focus": "museum",
                "nearby": False,
            }
        ]
        if explicit_replacement
        else [],
    )

    async def generate():
        return await ai.generate_edited_trip_plan(
            preferences=prefs,
            current_preferences=prefs,
            travel_context=context,
            current_plan=saved,
            instruction="Днём замени парк на музей. Остальные активности оставь.",
            analysis=parsed,
        )

    if explicit_replacement:
        result = await generate()
        assert result.days[0].afternoon[0].startswith("Другой музей: посетить музей.")
        assert result.days[0].morning == saved.days[0].morning
        assert result.days[0].evening == saved.days[0].evening
        assert "unfulfilled_place_replacement" in caplog.text
    else:
        # Без структурированной замены обычное правило интересов остаётся в силе.
        with pytest.raises(ai.AIServiceError):
            await generate()
        assert "missing_afternoon_interest" in caplog.text
    assert len(requests) == 2
    assert before == (
        saved.model_dump_json(),
        prefs.model_dump_json(),
        context.model_dump_json(),
    )
