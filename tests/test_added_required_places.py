"""Проверенные добавления дополняют обязательный список без второго вызова LLM."""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.trip import TripEditAnalysis
from app.services import ai
from scripts import evaluate_routes as evaluation
from tests.test_analysis_validation_diagnostics import edit_reply, install_transport


def prepare(payload=None, instruction=None):
    suite = evaluation.load_edit_suite()
    case = suite.cases[3]
    parsed = TripEditAnalysis.model_validate(payload or edit_reply())
    before = parsed.model_dump_json()
    result = ai._complete_added_required_places(
        analysis=parsed,
        current_preferences=case.preferences,
        instruction=instruction or case.steps[0].user_message,
        current_plan=suite.plans[case.current_plan],
    )
    assert parsed.model_dump_json() == before
    return result


@pytest.mark.parametrize("changed", [False, True])
def test_explicit_addition_completes_missing_required_name(changed):
    raw = edit_reply()
    raw.update(
        must_visit_places=["Art Aquarium Museum GINZA"],
        must_visit_places_changed=changed,
    )
    expected = deepcopy(raw)
    result = prepare(raw)
    assert result.must_visit_places == ["Art Aquarium Museum GINZA", "Тестовый музей 5"]
    assert result.must_visit_places_changed
    assert raw == expected
    assert prepare(result.model_dump()) == result


def test_complete_addition_is_unchanged():
    raw = edit_reply()
    assert prepare(raw).model_dump() == raw


@pytest.mark.parametrize(
    "defect",
    ["implicit", "removed", "unrelated", "undeclared", "duplicate", "day", "capacity"],
)
def test_completion_does_not_hide_other_invalid_changes(defect):
    raw = edit_reply()
    raw.update(
        must_visit_places=["Art Aquarium Museum GINZA"], must_visit_places_changed=False
    )
    if defect == "implicit":
        raw["place_additions"][0]["place_name"] = "PRIVATE_UNREQUESTED_VENUE"
    elif defect == "removed":
        raw.update(must_visit_places=[], must_visit_places_changed=True)
    elif defect in {"unrelated", "undeclared"}:
        raw["must_visit_places"].append("Тестовый музей 3")
        raw["must_visit_places_changed"] = defect == "unrelated"
    elif defect == "duplicate":
        raw["must_visit_places"].append("Art Aquarium Museum GINZA")
    elif defect == "day":
        raw["place_additions"][0]["day"] = 3
    else:
        raw["place_additions"] = [
            {**raw["place_additions"][0], "place_name": f"Новый музей {i}"}
            for i in range(5)
        ]
    instruction = (
        None
        if defect != "capacity"
        else "Добавь " + ", ".join(f"Новый музей {i}" for i in range(5))
    )
    with pytest.raises((ValueError, ValidationError)):
        prepare(raw, instruction)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_real_analyzer_and_local_eval_accept_omission_on_first_call(
    monkeypatch, changed
):
    raw = edit_reply()
    raw.update(
        must_visit_places=["Art Aquarium Museum GINZA"],
        must_visit_places_changed=changed,
    )
    requests = install_transport(monkeypatch, [json.dumps(raw)])
    suite = evaluation.load_edit_suite()
    result = await evaluation.evaluate_case(suite.cases[3], suite)
    assert result["status"] == "passed", result
    assert len(requests) == len(result["calls"]) == 1
    assert not result["retry_used"]
    assert all(result["checks"].values())
    analysis = result["steps"][0]["analysis"]
    assert analysis["must_visit_places"] == [
        "Art Aquarium Museum GINZA",
        "Тестовый музей 5",
    ]
    assert (
        result["steps"][0]["updated_preferences"]["must_visit_places"]
        == analysis["must_visit_places"]
    )
