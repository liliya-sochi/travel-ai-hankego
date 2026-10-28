"""Причины несовместимых полей различаются без ослабления схемы или утечки данных."""

import json
from itertools import product

import pytest
from pydantic import ValidationError

from app.schemas.trip import TripEditAnalysis
from app.services.ai_diagnostics import (
    analysis_validation_details,
    safe_analysis_validation_details,
)
from scripts import evaluate_routes as evaluation
from tests.test_analysis_validation_diagnostics import edit_reply, install_transport

PRIVATE = "PRIVATE_MODEL_VALUE_MUST_NOT_LEAK"


def conflicting_reply(interests=False, replacement=False, required=False):
    data = edit_reply()
    data.update(
        interests_changed=interests,
        interests=PRIVATE,
        must_visit_places_changed=required,
        place_additions=[],
        place_removals=[],
        place_replacements=[
            {
                "day": 1,
                "period": "afternoon",
                "place_name": PRIVATE,
                "activity_focus": None,
                "nearby": False,
            }
        ]
        if replacement
        else [],
    )
    return data


@pytest.mark.parametrize(
    ("interests", "replacement", "required"), list(product([False, True], repeat=3))
)
def test_local_mode_acceptance_is_unchanged_with_precise_reason(
    interests, replacement, required
):
    data = conflicting_reply(interests, replacement, required)
    if not any((interests, replacement, required)):
        assert not TripEditAnalysis.model_validate(data).requires_regeneration
        return
    reason = (
        "generation_required_interests"
        if interests
        else "generation_required_replacements"
        if replacement
        else "generation_required_required_places"
    )
    with pytest.raises(ValidationError) as caught:
        TripEditAnalysis.model_validate_json(json.dumps(data))
    details = analysis_validation_details(caught.value, stage="schema")
    assert details == {
        "validation_stage": "schema",
        "validation_reason": reason,
        "validation_issues": [
            {"field": "<root>", "error_type": "value_error", "reason": reason}
        ],
    }
    assert PRIVATE not in json.dumps(details)
    data.update(
        requires_regeneration=True,
        regeneration_periods=[{"day": 1, "period": "afternoon"}],
    )
    assert TripEditAnalysis.model_validate(data).requires_regeneration


@pytest.mark.parametrize("local_visit", ["place_additions", "place_removals"])
def test_local_required_changes_with_declared_visits_remain_valid(local_visit):
    data = conflicting_reply(required=True)
    data[local_visit] = [
        {
            "day": 1,
            "period": "afternoon",
            "place_name": PRIVATE,
            **({"activity_focus": None} if local_visit == "place_additions" else {}),
        }
    ]
    assert TripEditAnalysis.model_validate(data).must_visit_places_changed


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        ((True, False, False), "generation_required_interests"),
        ((False, True, False), "generation_required_replacements"),
        ((False, False, True), "generation_required_required_places"),
    ],
)
def test_precise_reason_survives_real_retry_and_written_report(
    monkeypatch, tmp_path, flags, reason
):
    requests = install_transport(
        monkeypatch, [json.dumps(conflicting_reply(*flags)), "rate_limit", "rate_limit"]
    )
    path = tmp_path / "report.json"
    assert (
        evaluation.main(
            [
                "--suite",
                "edits",
                "--live",
                "--case",
                "tokyo_rest_edit_cycle",
                "--repeat",
                "1",
                "--output",
                str(path),
            ]
        )
        == 1
    )
    report = json.loads(path.read_text())
    result = report["results"][0]
    assert result["status"] == "service_error"
    assert len(requests) == len(result["calls"]) == 3
    assert [c["outcome"] for c in result["calls"]] == [
        "invalid_output",
        "rate_limited",
        "rate_limited",
    ]
    assert result["calls"][0]["validation_reason"] == reason
    assert result["steps"][0]["calls"][0]["validation_reason"] == reason
    assert PRIVATE not in path.read_text()


def test_legacy_combined_reason_remains_safe_for_old_reports():
    result = safe_analysis_validation_details(
        {
            "validation_stage": "schema",
            "validation_reason": "generation_required",
            "validation_issues": [],
        }
    )
    assert result["validation_reason"] == "generation_required"
