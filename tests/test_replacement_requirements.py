"""Тип, расстояние и независимые кандидаты явно запрошенных замен."""

import json
import math
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.geoapify import TravelContext
from app.schemas.trip import TripEditAnalysis, TripPlaceReplacement, TripPlanResponse
from app.services import ai
from app.services.place_geography import EARTH_RADIUS_METERS
from app.services.trip_editing import saved_visit_reference
from scripts import evaluate_routes as evaluation

ReplacementCase = tuple[
    evaluation.EditEvaluationCase, TravelContext, TripPlanResponse, TripEditAnalysis
]


@pytest.fixture
def replacement_case() -> ReplacementCase:
    """Все координаты, ID и расписания взяты из синтетического edit-корпуса."""

    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    context = suite.contexts[case.context]
    saved = suite.plans[case.current_plan]
    parsed = TripEditAnalysis(
        supported=True,
        interests_changed=False,
        interests=None,
        must_visit_places_changed=False,
        must_visit_places=case.preferences.must_visit_places,
        rest_period_changes=[],
        period_actions=[],
        requires_regeneration=True,
        regeneration_periods=[{"day": 2, "period": "afternoon"}],
        place_removals=[],
        place_additions=[],
        place_replacements=[
            dict(
                day=2,
                period="afternoon",
                place_name="Тестовый музей 4",
                activity_focus="museum",
                nearby=True,
            )
        ],
    )
    return case, context, saved, parsed


def generated_plan(
    context: TravelContext, target_id: str, focus: str = "museum"
) -> dict[str, Any]:
    """Ответ провайдера с явными ID; сохранение остальных клеток проверяет приложение."""

    places = {place.source_place_id: place for place in context.places}

    def visit(place_id: str, activity_focus: str = "museum") -> dict[str, Any]:
        return dict(
            source_place_id=place_id,
            place_name=places[place_id].name,
            activity_focus=activity_focus,
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
                morning=[
                    visit("eval-edit-aquarium" if day == 1 else "eval-edit-museum-3")
                ],
                afternoon=[
                    visit(
                        "eval-edit-aida" if day == 1 else target_id,
                        "museum" if day == 1 else focus,
                    )
                ],
                evening=[
                    dict(
                        source_place_id=None,
                        place_name=None,
                        activity_focus=None,
                        description="Другая прогулка.",
                    )
                ],
            )
            for day in (1, 2)
        ],
    )


def install_replies(
    monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Подменяет только внешнюю границу, сохраняя настоящий retry и валидатор."""

    calls: list[dict[str, Any]] = []

    async def request(**arguments: Any) -> ai.LLMProviderResponse:
        assert replies, "Незапланированный вызов LLM."
        calls.append(deepcopy(arguments["payload"]))
        return ai.LLMProviderResponse(
            data={
                "choices": [
                    {
                        "message": {"content": json.dumps(replies.pop(0))},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
            duration_ms=1,
            header_request_id="test-replacement",
        )

    monkeypatch.setattr(ai, "_request_model", request)
    return calls


async def generate(data: ReplacementCase) -> TripPlanResponse:
    case, context, saved, parsed = data
    return await ai.generate_edited_trip_plan(
        preferences=case.preferences,
        current_preferences=case.preferences,
        current_plan=saved,
        travel_context=context,
        instruction=case.steps[0].user_message,
        analysis=parsed,
    )


@pytest.mark.parametrize(
    "defect", ["missing_focus", "missing_nearby", "unknown_focus", "string_flag"]
)
def test_replacement_conditions_are_required_and_strict(defect: str) -> None:
    values = dict(
        day=1, period="morning", place_name="Музей", activity_focus=None, nearby=False
    )
    if defect == "missing_focus":
        values.pop("activity_focus")
    elif defect == "missing_nearby":
        values.pop("nearby")
    elif defect == "unknown_focus":
        values["activity_focus"] = "shopping"
    else:
        values["nearby"] = "false"
    with pytest.raises(ValidationError):
        TripPlaceReplacement.model_validate(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["far", "wrong_category", "wrong_focus"])
async def test_replacement_conditions_use_existing_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    replacement_case: ReplacementCase,
    defect: str,
) -> None:
    case, context, saved, _parsed = replacement_case
    if defect == "far":
        target_id, focus = "eval-edit-far-museum", "museum"
    elif defect == "wrong_category":
        target_id, focus = "eval-edit-near-park", "park"
    else:
        target_id, focus = "eval-edit-museum-5", "architecture"
        next(
            p for p in context.places if p.source_place_id == target_id
        ).categories.append("building.tourism")
    before = (
        context.model_dump_json(),
        saved.model_dump_json(),
        case.preferences.model_dump_json(),
    )
    calls = install_replies(
        monkeypatch,
        [
            generated_plan(context, target_id, focus),
            generated_plan(context, "eval-edit-museum-5"),
        ],
    )
    result = await generate(replacement_case)
    assert len(calls) == 2
    assert "invalid_replacement_choice" in caplog.text
    incoming = json.loads(calls[0]["messages"][1]["content"])
    assert incoming["place_replacements"][0]["visit_requirements"][0][
        "allowed_source_place_ids"
    ] == ["eval-edit-museum-5"]
    assert result.days[1].afternoon[0].startswith("Тестовый музей 5: посетить музей.")
    for day, old in zip(result.days, saved.days, strict=True):
        assert [saved_visit_reference(line) for line in day.morning] == [
            saved_visit_reference(line) for line in old.morning
        ]
        assert day.evening == old.evening
    assert [saved_visit_reference(line) for line in result.days[0].afternoon] == [
        saved_visit_reference(line) for line in saved.days[0].afternoon
    ]
    assert before == (
        context.model_dump_json(),
        saved.model_dump_json(),
        case.preferences.model_dump_json(),
    )


@pytest.mark.asyncio
async def test_invalid_replacement_twice_returns_no_plan(
    monkeypatch: pytest.MonkeyPatch, replacement_case: ReplacementCase
) -> None:
    _, context, saved, _ = replacement_case
    before = saved.model_dump_json()
    bad = generated_plan(context, "eval-edit-far-museum")
    calls = install_replies(monkeypatch, [bad, bad])
    with pytest.raises(ai.AIServiceError):
        await generate(replacement_case)
    assert len(calls) == 2
    assert saved.model_dump_json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "missing_candidate",
        "closed",
        "wrong_category",
        "missing_anchor",
        "ambiguous_anchor",
    ],
)
async def test_unverifiable_nearby_replacement_stops_before_generation(
    monkeypatch: pytest.MonkeyPatch, replacement_case: ReplacementCase, defect: str
) -> None:
    _, context, saved, _ = replacement_case
    candidate = next(
        p for p in context.places if p.source_place_id == "eval-edit-museum-5"
    )
    if defect == "missing_candidate":
        context.places.remove(candidate)
    elif defect == "closed":
        candidate.opening_hours = "off"
    elif defect == "wrong_category":
        candidate.categories = ["leisure.park"]
    elif defect == "missing_anchor":
        context.places = [
            p for p in context.places if p.source_place_id != "eval-edit-museum-4"
        ]
    else:
        anchor = next(
            p for p in context.places if p.source_place_id == "eval-edit-museum-4"
        )
        context.places.append(
            anchor.model_copy(update={"source_place_id": "ambiguous-anchor"})
        )
    before = saved.model_dump_json(), context.model_dump_json()
    calls = install_replies(monkeypatch, [])
    with pytest.raises(
        ai.AIServiceError, match=r"Маршрут не изменён|маршрут не изменён"
    ):
        await generate(replacement_case)
    assert not calls
    assert before == (saved.model_dump_json(), context.model_dump_json())


@pytest.mark.asyncio
@pytest.mark.parametrize("distance", [1999, 2001])
async def test_nearby_replacement_uses_distance_instead_of_area_label(
    monkeypatch: pytest.MonkeyPatch, replacement_case: ReplacementCase, distance: int
) -> None:
    _, context, _, _ = replacement_case
    anchor = next(
        p for p in context.places if p.source_place_id == "eval-edit-museum-4"
    )
    candidate = next(
        p for p in context.places if p.source_place_id == "eval-edit-museum-5"
    )
    candidate.latitude = anchor.latitude + math.degrees(distance / EARTH_RADIUS_METERS)
    candidate.longitude = anchor.longitude
    calls = install_replies(
        monkeypatch, [generated_plan(context, "eval-edit-museum-5")]
    )
    if distance < 2000:
        assert (
            (await generate(replacement_case))
            .days[1]
            .afternoon[0]
            .startswith("Тестовый музей 5:")
        )
        assert len(calls) == 1
    else:
        with pytest.raises(ai.AIServiceError):
            await generate(replacement_case)
        assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_conditions_are_not_invented_for_unconstrained_replacement(
    monkeypatch: pytest.MonkeyPatch, replacement_case: ReplacementCase, typed: bool
) -> None:
    _, context, _, parsed = replacement_case
    request = parsed.place_replacements[0]
    request.activity_focus = "museum" if typed else None
    request.nearby = not typed
    target_id, focus = (
        ("eval-edit-far-museum", "museum") if typed else ("eval-edit-near-park", "park")
    )
    calls = install_replies(monkeypatch, [generated_plan(context, target_id, focus)])
    result = await generate(replacement_case)
    assert len(calls) == 1
    assert (
        next(p for p in context.places if p.source_place_id == target_id).name
        in result.days[1].afternoon[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enough_choices", [False, True])
async def test_two_replacements_require_distinct_suitable_candidates(
    monkeypatch: pytest.MonkeyPatch,
    replacement_case: ReplacementCase,
    enough_choices: bool,
) -> None:
    _, context, saved, parsed = replacement_case
    anchor = next(
        p for p in context.places if p.source_place_id == "eval-edit-museum-4"
    )
    context.places.append(
        anchor.model_copy(
            update={"source_place_id": "old-6", "name": "Прежний музей 6"}
        )
    )
    saved.days[1].afternoon.append("Прежний музей 6: посетить музей.")
    parsed.place_replacements.append(
        parsed.place_replacements[0].model_copy(
            update={"place_name": "Прежний музей 6"}
        )
    )
    if enough_choices:
        # Первой замене подходит и музей, и парк, второй — только музей.
        parsed.place_replacements[0].activity_focus = None
    reply = generated_plan(context, "eval-edit-museum-5")
    park = next(p for p in context.places if p.source_place_id == "eval-edit-near-park")
    reply["days"][1]["afternoon"].append(
        dict(
            source_place_id=park.source_place_id,
            place_name=park.name,
            activity_focus="park",
            description=None,
        )
    )
    calls = install_replies(monkeypatch, [reply])
    if enough_choices:
        assert len((await generate(replacement_case)).days[1].afternoon) == 2
        assert len(calls) == 1
    else:
        with pytest.raises(ai.AIServiceError):
            await generate(replacement_case)
        assert not calls


@pytest.mark.asyncio
async def test_interest_alignment_cannot_change_preserved_focus(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    replacement_case: ReplacementCase,
) -> None:
    case, context, saved, _ = replacement_case
    case.preferences.interests = "Музеи и архитектура"
    saved.days[1].afternoon = ["Тестовый музей 4: осмотреть архитектурный объект."]
    next(
        p for p in context.places if p.source_place_id == "eval-edit-museum-4"
    ).categories.append("building.tourism")
    before = saved.model_dump_json()
    reply = generated_plan(context, "eval-edit-museum-5")
    calls = install_replies(monkeypatch, [reply, reply])
    with pytest.raises(ai.AIServiceError):
        await generate(replacement_case)
    assert len(calls) == 2
    assert "missing_interest_category" in caplog.text
    assert saved.model_dump_json() == before


@pytest.mark.asyncio
async def test_sparse_eligible_replacement_stays_in_payload_and_schema(
    monkeypatch: pytest.MonkeyPatch, replacement_case: ReplacementCase
) -> None:
    case, context, _, _ = replacement_case
    candidate = next(
        p for p in context.places if p.source_place_id == "eval-edit-museum-5"
    )
    candidate.opening_hours = None
    candidate.available_details = []
    candidate.website = None
    calls = install_replies(
        monkeypatch, [generated_plan(context, candidate.source_place_id)]
    )
    assert (
        (await generate(replacement_case))
        .days[1]
        .afternoon[0]
        .startswith("Тестовый музей 5:")
    )
    incoming = json.loads(calls[0]["messages"][1]["content"])
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert (
        candidate.source_place_id
        in schema["$defs"]["GroundedActivity"]["properties"]["source_place_id"][
            "anyOf"
        ][0]["enum"]
    )
    assert candidate.source_place_id in {
        p["source_place_id"] for p in incoming["travel_context"]["places"]
    }
    assert (
        candidate.source_place_id
        not in incoming["travel_context"]["must_visit_place_ids"]
    )
    assert case.preferences.must_visit_places == ["Art Aquarium Museum GINZA"]
