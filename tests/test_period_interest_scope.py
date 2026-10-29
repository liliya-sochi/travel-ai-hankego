"""Локальные правки периода сохраняют интересы; общая смена тем требует генерации."""

import json

import pytest

from app.services import ai
from scripts import evaluate_routes as evaluation
from tests.test_analysis_validation_diagnostics import edit_reply, install_transport


def scoped_reply(*, rest=None, walk=False):
    payload = edit_reply()
    payload.update(
        must_visit_places_changed=False,
        must_visit_places=["Art Aquarium Museum GINZA"],
        place_additions=[],
        rest_period_changes=[]
        if rest is None
        else [{"day": None, "period": "evening", "rest": rest}],
        period_actions=[{"day": None, "period": "evening", "action": "quiet_walk"}]
        if walk
        else [],
    )
    return payload


@pytest.mark.asyncio
async def test_three_real_analysis_turns_keep_interests_and_apply_locally(monkeypatch):
    requests = install_transport(
        monkeypatch,
        [
            json.dumps(payload)
            for payload in (
                scoped_reply(rest=True),
                scoped_reply(),
                scoped_reply(rest=False, walk=True),
            )
        ],
    )
    suite = evaluation.load_edit_suite()
    case = suite.cases[0].model_copy(deep=True)
    for step in case.steps:
        step.expectations.generation_expected = False
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed", result
    assert len(requests) == len(result["calls"]) == 3
    assert not result["retry_used"]
    for turn in result["steps"]:
        assert turn["checks"]["interests_change_scope"]
        assert turn["checks"]["interests_preserved"]
        assert turn["checks"]["generation_expected"]
        assert (
            turn["updated_preferences"]["interests"]
            == suite.cases[0].preferences.interests
        )
    assert all(
        request["response_format"]["json_schema"]["name"] == "trip_edit_analysis"
        for request in requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_rest", [False, True])
async def test_trip_wide_theme_change_and_mixed_change_remain_supported(
    monkeypatch, with_rest
):
    payload = scoped_reply(rest=True if with_rest else None)
    payload.update(
        interests_changed=True,
        interests="Парки",
        requires_regeneration=True,
        regeneration_periods=[
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
    )
    requests = install_transport(monkeypatch, [json.dumps(payload)])
    suite = evaluation.load_edit_suite()
    case = suite.cases[0]
    instruction = "Вместо музеев хочу парки." + (
        " Вечером спокойный отдых." if with_rest else ""
    )
    result = await ai.analyze_trip_edit(
        current_preferences=case.preferences,
        current_plan=suite.plans[case.current_plan],
        instruction=instruction,
    )
    assert result.interests_changed and result.requires_regeneration
    merged = evaluation.merge_edit_preferences(case.preferences, result)
    assert merged.interests == "Парки"
    assert bool(merged.rest_periods) == with_rest
    assert len(requests) == 1
    assert (
        json.loads(requests[0]["messages"][1]["content"])["edit_instruction"]
        == instruction
    )


@pytest.mark.asyncio
async def test_independent_rest_eval_rejects_trip_wide_interests_rewrite(monkeypatch):
    payload = scoped_reply(rest=True)
    payload.update(
        interests_changed=True,
        interests="Музеи; спокойный отдых",
        requires_regeneration=True,
        regeneration_periods=[
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
    )
    requests = install_transport(monkeypatch, [json.dumps(payload)])
    suite = evaluation.load_edit_suite()
    case = suite.cases[0].model_copy(deep=True)
    case.steps = case.steps[:1]
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "quality_failed"
    assert not result["checks"]["step:1:interests_change_scope"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_contradictory_reply_still_rejected_before_valid_local_retry(monkeypatch):
    bad = scoped_reply(rest=True)
    bad.update(interests_changed=True, interests="Музеи; спокойный отдых")
    requests = install_transport(
        monkeypatch, [json.dumps(bad), json.dumps(scoped_reply(rest=True))]
    )
    suite = evaluation.load_edit_suite()
    case = suite.cases[0].model_copy(deep=True)
    case.steps = case.steps[:1]
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed"
    assert result["calls"][0]["validation_reason"] == "generation_required_interests"
    assert len(requests) == 2
