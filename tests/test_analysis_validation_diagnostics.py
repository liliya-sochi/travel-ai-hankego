"""Безопасная диагностика настоящих анализаторов и её перенос в eval-отчёты."""

import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.trip import TripEditAnalysis, TripIntakeExtraction
from app.services import ai
from app.services.ai_diagnostics import (
    MAX_VALIDATION_ISSUES,
    analysis_validation_details,
    safe_analysis_validation_details,
)
from scripts import evaluate_routes as evaluation

PRIVATE_REPLY = "PRIVATE_MODEL_REPLY_DO_NOT_LOG"
PRIVATE_KEY = "PRIVATE_API_KEY_DO_NOT_LOG"


def edit_reply() -> dict[str, Any]:
    """Валидное именованное добавление в существующий период."""

    return {
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
        "place_removals": [],
        "place_additions": [
            {
                "day": 2,
                "period": "afternoon",
                "place_name": "Тестовый музей 5",
                "activity_focus": None,
            }
        ],
    }


@pytest.mark.parametrize(
    ("defect", "reason", "field", "kind"),
    [
        ("generation", "generation_required", "<root>", "value_error"),
        ("mode", "regeneration_mode_mismatch", "<root>", "value_error"),
        ("missing", "schema_validation", "place_additions[].activity_focus", "missing"),
        ("literal", "schema_validation", "place_additions[].period", "literal_error"),
        ("extra", "schema_validation", "<other>", "extra_forbidden"),
    ],
)
def test_real_schema_errors_have_safe_codes_and_fields(defect, reason, field, kind):
    data = edit_reply()
    data["interests"] = PRIVATE_REPLY
    if defect == "generation":
        data["interests_changed"] = True
    elif defect == "mode":
        data["regeneration_periods"] = [{"day": 2, "period": "afternoon"}]
    elif defect == "missing":
        del data["place_additions"][0]["activity_focus"]
    elif defect == "literal":
        data["place_additions"][0]["period"] = PRIVATE_REPLY
    else:
        data[PRIVATE_KEY] = PRIVATE_REPLY
    with pytest.raises(ValidationError) as caught:
        TripEditAnalysis.model_validate_json(json.dumps(data))
    result = analysis_validation_details(caught.value, stage="schema")
    assert result == {
        "validation_stage": "schema",
        "validation_reason": reason,
        "validation_issues": [{"field": field, "error_type": kind, "reason": reason}],
    }
    assert "PRIVATE" not in json.dumps(result)


def test_unknown_exception_and_pydantic_context_are_not_exposed():
    unknown = analysis_validation_details(ValueError(PRIVATE_REPLY), stage="semantic")
    assert unknown == {
        "validation_stage": "semantic",
        "validation_reason": "other_analysis_validation",
        "validation_issues": [],
    }
    error = ValidationError.from_exception_data(
        "PrivateModel",
        [
            {
                "type": "value_error",
                "loc": ("place_additions", 139760000, PRIVATE_KEY),
                "input": PRIVATE_REPLY,
                "ctx": {"error": ValueError(PRIVATE_REPLY)},
            }
        ],
    )
    result = analysis_validation_details(error, stage="schema")
    assert result["validation_issues"][0]["field"] == "place_additions[].<other>"
    assert "PRIVATE" not in json.dumps(result)
    assert "139760000" not in json.dumps(result)


def test_missing_fields_are_bounded_and_do_not_include_values():
    with pytest.raises(ValidationError) as caught:
        TripEditAnalysis.model_validate_json("{}")
    result = analysis_validation_details(caught.value, stage="schema")
    assert len(result["validation_issues"]) == MAX_VALIDATION_ISSUES
    assert all(item["error_type"] == "missing" for item in result["validation_issues"])
    assert set(result) == {"validation_stage", "validation_reason", "validation_issues"}


@pytest.mark.parametrize("stage", ["schema", "semantic", "extraction", PRIVATE_REPLY])
def test_log_and_eval_collector_resanitize_untrusted_diagnostic_values(caplog, stage):
    raw = {
        "validation_stage": stage,
        "validation_reason": PRIVATE_REPLY,
        "validation_issues": [
            {
                "field": PRIVATE_KEY,
                "error_type": PRIVATE_REPLY,
                "reason": PRIVATE_REPLY,
                "input": PRIVATE_REPLY,
            }
        ]
        * 20,
        "prompt": PRIVATE_REPLY,
    }
    before = deepcopy(raw)
    metadata = ai.LLMResponseMetadata("fixture-analysis", "fixture-id", 1, 2, 3, "stop")
    with caplog.at_level(logging.INFO, logger=ai.__name__):
        ai._log_llm_call(
            level=logging.WARNING,
            outcome="invalid_output",
            metadata=metadata,
            attempt=1,
            duration_ms=1,
            validation_details=raw,
        )
    event = next(
        json.loads(r.args[0]) for r in caplog.records if r.msg == "LLM call | %s"
    )
    assert "PRIVATE" not in json.dumps(event)
    collector = evaluation.CallCollector()
    collector.emit(
        logging.LogRecord(
            "test",
            logging.WARNING,
            "",
            0,
            "LLM call | %s",
            (json.dumps({"outcome": "invalid_output", **raw}),),
            None,
        )
    )
    assert "PRIVATE" not in json.dumps(collector.calls)
    assert collector.calls[0]["validation_reason"] == "other_analysis_validation"
    assert collector.calls[0]["validation_issues"] == [
        {
            "field": "<other>",
            "error_type": "validation_error",
            "reason": "schema_validation",
        }
    ]
    assert raw == before


def test_old_grounded_reason_is_preserved_without_analysis_details():
    collector = evaluation.CallCollector()
    collector.emit(
        logging.LogRecord(
            "test",
            logging.WARNING,
            "",
            0,
            "LLM call | %s",
            (
                json.dumps(
                    {
                        "outcome": "semantic_validation_failed",
                        "validation_reason": "distant_daily_places",
                    }
                ),
            ),
            None,
        )
    )
    assert collector.calls == [
        {
            "outcome": "semantic_validation_failed",
            "validation_reason": "distant_daily_places",
        }
    ]
    assert safe_analysis_validation_details(None) == {}


def install_transport(monkeypatch, replies: list[object]):
    """Подменяет только HTTP, сохраняя retries, валидацию и логирование."""

    requests = []
    queue = list(replies)

    def respond(request):
        requests.append(json.loads(request.content))
        reply = queue.pop(0)
        if reply == "rate_limit":
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": {"message": PRIVATE_REPLY}},
            )
        return httpx.Response(
            200,
            json={
                "model": "fixture-analysis",
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
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
        llm_api_key=PRIVATE_KEY,
        llm_analysis_model="fixture-analysis",
        llm_model="fixture-generation",
    )
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    monkeypatch.setattr(ai.asyncio, "sleep", AsyncMock())
    return requests


@pytest.mark.parametrize(
    ("defect", "stage", "reason"),
    [
        ("generation", "schema", "generation_required"),
        ("missing", "schema", "schema_validation"),
        ("semantic", "semantic", "required_change_absent_from_instruction"),
        ("json", "schema", "schema_validation"),
        ("extraction", "extraction", "model_text_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_edit_retry_keeps_diagnostic_then_succeeds_without_generation(
    monkeypatch, caplog, defect, stage, reason
):
    bad = edit_reply()
    bad["interests"] = PRIVATE_REPLY
    if defect == "generation":
        bad["interests_changed"] = True
    elif defect == "missing":
        del bad["place_additions"][0]["activity_focus"]
    elif defect == "semantic":
        bad["must_visit_places"].append(PRIVATE_REPLY)
    bad_text = (
        None
        if defect == "extraction"
        else PRIVATE_REPLY
        if defect == "json"
        else json.dumps(bad)
    )
    requests = install_transport(monkeypatch, [bad_text, json.dumps(edit_reply())])
    suite = evaluation.load_edit_suite()
    with caplog.at_level(logging.INFO, logger=ai.__name__):
        result = await evaluation.evaluate_case(suite.cases[3], suite)
    assert result["status"] == "passed", result
    assert len(requests) == len(result["calls"]) == 2
    first, second = result["calls"]
    assert first["outcome"] == "invalid_output"
    assert first["validation_stage"] == stage
    assert first["validation_reason"] == reason
    assert "validation_stage" not in second
    assert result["checks"]["step:1:generation_expected"]
    assert requests[1]["messages"][-1]["content"] == ai.EDIT_ANALYSIS_RETRY_PROMPT
    assert all(
        p["response_format"]["json_schema"]["name"] == "trip_edit_analysis"
        for p in requests
    )
    assert "PRIVATE" not in json.dumps(result) + caplog.text


def test_diagnostic_survives_two_rate_limits_in_written_live_report(
    monkeypatch, tmp_path
):
    bad = edit_reply()
    bad.update(interests_changed=True, interests=PRIVATE_REPLY)
    requests = install_transport(
        monkeypatch, [json.dumps(bad), "rate_limit", "rate_limit"]
    )
    output = tmp_path / "report.json"
    assert (
        evaluation.main(
            [
                "--suite",
                "edits",
                "--live",
                "--case",
                "tokyo_add_second_afternoon",
                "--repeat",
                "1",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text())
    result = report["results"][0]
    assert report["completed"] and result["status"] == "service_error"
    assert result["error_type"] == "AIProviderRateLimitError"
    assert len(requests) == len(result["calls"]) == 3
    assert [call["outcome"] for call in result["calls"]] == [
        "invalid_output",
        "rate_limited",
        "rate_limited",
    ]
    assert result["calls"][0]["validation_reason"] == "generation_required"
    assert all("validation_stage" not in call for call in result["calls"][1:])
    assert result["steps"][0]["calls"][0] == result["calls"][0]
    assert "PRIVATE" not in output.read_text()


@pytest.mark.parametrize("bad_text", [None, "{}", PRIVATE_REPLY])
@pytest.mark.asyncio
async def test_intake_failures_have_same_safe_contract_and_bounded_retry(
    monkeypatch, bad_text
):
    valid = {
        "intent": "unknown",
        "destination": None,
        "duration_days": None,
        "travel_period": None,
        "budget": None,
        "interests": None,
        "must_visit_places": None,
    }
    requests = install_transport(monkeypatch, [bad_text, json.dumps(valid)])
    collector = evaluation.CallCollector()
    ai.logger.addHandler(collector)
    try:
        result = await ai.analyze_trip_message(
            user_message="Привет", draft=ai.TripDraft()
        )
    finally:
        ai.logger.removeHandler(collector)
    assert isinstance(result, TripIntakeExtraction)
    assert len(requests) == len(collector.calls) == 2
    first = collector.calls[0]
    assert first["validation_stage"] == ("extraction" if bad_text is None else "schema")
    assert "PRIVATE" not in json.dumps(collector.calls)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("place_additions", "addition_day_outside_trip"),
        ("regeneration_periods", "regeneration_day_outside_trip"),
        ("period_actions", "action_day_outside_trip"),
        ("rest_period_changes", "rest_day_outside_trip"),
    ],
)
def test_actual_out_of_trip_checks_have_specific_safe_reasons(field, reason):
    suite = evaluation.load_edit_suite()
    case = suite.cases[3]
    data = edit_reply()
    if field == "place_additions":
        data[field][0]["day"] = 3
    elif field == "regeneration_periods":
        data.update(
            requires_regeneration=True,
            regeneration_periods=[{"day": 3, "period": "morning"}],
        )
    elif field == "period_actions":
        data[field] = [{"day": 3, "period": "morning", "action": "preserve"}]
    else:
        data[field] = [{"day": 3, "period": "morning", "rest": True}]
    parsed = TripEditAnalysis.model_validate(data)
    with pytest.raises(ValueError) as caught:
        ai._validate_trip_edit_analysis(
            analysis=parsed,
            current_preferences=case.preferences,
            current_plan=suite.plans[case.current_plan],
            instruction=case.steps[0].user_message,
        )
    result = analysis_validation_details(caught.value, stage="semantic")
    assert result == {
        "validation_stage": "semantic",
        "validation_reason": reason,
        "validation_issues": [],
    }
