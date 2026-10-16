"""Проверки последовательных edit-evals без настоящей сети и токенов."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.schemas.trip import RestPeriodChange, TripEditAnalysis
from app.services import ai
from scripts import evaluate_routes as evaluation


@pytest.fixture
def suite() -> evaluation.EditEvaluationSuite:
    return evaluation.load_edit_suite()


class DummyAsyncClient:
    """Не открывает HTTP-соединение: внешние байты подменяются отдельно."""

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def mock_provider(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Оставляет настоящими AI-функции, merge, валидацию и форматирование."""

    requests: list[dict[str, Any]] = []
    turn = 0
    replacement_mode = False

    async def request(**arguments: Any) -> ai.LLMProviderResponse:
        nonlocal turn, replacement_mode
        payload = arguments["payload"]
        incoming = json.loads(payload["messages"][1]["content"])
        requests.append(incoming)
        if "current_preferences" in incoming:
            turn += 1
            replacement_mode = "замени музей" in incoming["edit_instruction"]
            duration = incoming["current_preferences"]["duration_days"]
            stage = (turn - 1) % 3
            changes = (
                []
                if stage == 1
                else [
                    {
                        "day": 1 if duration == 1 else 2,
                        "period": "evening",
                        "rest": stage == 0,
                    }
                ]
            )
            content = {
                "period_actions": [],
                "requires_regeneration": True,
                "regeneration_periods": [
                    {"day": None, "period": period}
                    for period in ("morning", "afternoon", "evening")
                ],
                "supported": True,
                "interests_changed": True,
                "interests": f"Музеи; фактическая метка {turn}",
                "must_visit_places_changed": False,
                "must_visit_places": incoming["current_preferences"][
                    "must_visit_places"
                ],
                "rest_period_changes": changes,
            }
            if replacement_mode:
                content.update(
                    interests_changed=False,
                    interests=None,
                    rest_period_changes=[],
                    regeneration_periods=[{"day": 2, "period": "afternoon"}],
                )
        else:
            duration = incoming["trip_preferences"]["duration_days"]
            places = incoming["travel_context"]["places"]
            by_name = {place["name"]: place for place in places}
            names = [
                "Art Aquarium Museum GINZA",
                "Mitsuo Aida Museum",
                "Тестовый музей 3",
                "Тестовый музей 4",
            ]
            if replacement_mode:
                names[-1] = "Тестовый музей 5"

            def visit(index: int) -> dict[str, Any]:
                place = by_name[names[index]]
                return {
                    "source_place_id": place["source_place_id"],
                    "place_name": place["name"],
                    "activity_focus": "museum",
                    "description": None,
                }

            def general(description: str) -> dict[str, Any]:
                return {
                    "source_place_id": None,
                    "place_name": None,
                    "activity_focus": None,
                    "description": description,
                }

            content = {
                "destination": "Токио",
                "duration_days": duration,
                "summary": "План поездки",
                "days": [
                    {
                        "day": day,
                        "title": "План дня",
                        "morning": [visit((day - 1) * 2)],
                        "afternoon": [
                            visit((day - 1) * 2 + 1),
                            general(f"Неспешная пауза {turn}."),
                        ],
                        "evening": [general("Спокойная прогулка.")],
                    }
                    for day in range(1, duration + 1)
                ],
            }
            if replacement_mode:
                for day in content["days"]:
                    day["afternoon"] = day["afternoon"][:1]
                content["days"][0]["afternoon"][0]["activity_focus"] = "architecture"
        return ai.LLMProviderResponse(
            data={
                "model": payload["model"],
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
            duration_ms=1,
            header_request_id="fixture-request",
        )

    settings = SimpleNamespace(
        llm_base_url="https://example.invalid",
        llm_api_key="fixture-secret",
        llm_model="fixture-generation",
        llm_analysis_model="fixture-analysis",
    )
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(ai, "_request_model", request)
    monkeypatch.setattr(ai.httpx, "AsyncClient", DummyAsyncClient)
    return requests


def test_edit_corpus_has_rest_cycles_and_scoped_replacement(suite) -> None:
    assert [case.id for case in suite.cases] == [
        "tokyo_rest_edit_cycle",
        "tokyo_rest_second_day",
        "tokyo_replace_second_afternoon",
    ]
    assert [len(case.steps) for case in suite.cases] == [3, 3, 1]
    assert suite.contexts["tokyo_museums"].fetched_at.tzinfo is not None


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "unknown_context",
        "unknown_plan",
        "unknown_id",
        "wrong_day",
        "duplicate_delta",
        "contradiction",
        "empty_term",
        "wrong_duration",
        "rest_visit_conflict",
        "text_visit_conflict",
    ],
)
def test_invalid_edit_corpus_is_rejected(suite, defect: str) -> None:
    data = suite.model_dump(mode="json")
    case = data["cases"][0]
    expected = case["steps"][0]["expectations"]
    if defect == "duplicate":
        data["cases"].append(case)
    elif defect == "unknown_context":
        case["context"] = "missing"
    elif defect == "unknown_plan":
        case["current_plan"] = "missing"
    elif defect == "unknown_id":
        case["preserved_visits"][0]["place_ids"] = ["missing"]
    elif defect == "wrong_day":
        expected["rest_period_changes"][0]["day"] = 2
    elif defect == "duplicate_delta":
        expected["rest_period_changes"].append(expected["rest_period_changes"][0])
    elif defect == "contradiction":
        expected["rest_periods"] = []
    elif defect == "empty_term":
        case["steps"][2]["expectations"]["general_activities"][0]["contains"] = [" "]
    elif defect == "wrong_duration":
        case["current_plan"] = "tokyo_2_days"
    elif defect == "rest_visit_conflict":
        case["preserved_visits"][0]["period"] = "evening"
    else:
        case["steps"][2]["expectations"]["general_activities"][0]["period"] = "morning"
    with pytest.raises(ValueError):
        evaluation.EditEvaluationSuite.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("case_index", [0, 1])
@pytest.mark.asyncio
async def test_real_ai_boundaries_carry_actual_state_between_edits(
    suite, monkeypatch: pytest.MonkeyPatch, case_index: int
) -> None:
    before = suite.model_dump_json()
    requests = mock_provider(monkeypatch)
    case = suite.cases[case_index]
    result = await evaluation.evaluate_case(case, suite)

    assert result["status"] == "passed", result
    assert len(requests) == 6
    assert len(result["calls"]) == 6
    assert result["known_total_tokens"] == 180
    assert result["usage_complete"] and not result["retry_used"]
    assert all(len(step["calls"]) == 2 for step in result["steps"])
    assert "failed_step" not in result
    for index, turn in enumerate(result["steps"][1:], start=1):
        previous = result["steps"][index - 1]
        assert turn["input_preferences"] == previous["updated_preferences"]
        assert turn["input_plan"] == previous["plan"]
        assert requests[index * 2]["current_preferences"]["interests"] == (
            f"Музеи; фактическая метка {index}"
        )
        assert (
            f"Неспешная пауза {index}."
            in requests[index * 2]["current_trip_plan"]["days"][0]["afternoon"]
        )
    assert result["steps"][-1]["updated_preferences"]["rest_periods"] == []
    assert suite.model_dump_json() == before


@pytest.mark.parametrize("supported", [True, False])
@pytest.mark.asyncio
async def test_bad_analysis_stops_before_generation_and_next_step(
    suite, monkeypatch: pytest.MonkeyPatch, supported: bool
) -> None:
    analysis = TripEditAnalysis(
        period_actions=[],
        requires_regeneration=True,
        regeneration_periods=[
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
        supported=supported,
        interests_changed=False,
        interests=None,
        must_visit_places_changed=False,
        must_visit_places=suite.cases[0].preferences.must_visit_places,
        rest_period_changes=[],
    )
    analyzer = AsyncMock(return_value=analysis)
    generator = AsyncMock()
    monkeypatch.setattr(ai, "analyze_trip_edit", analyzer)
    monkeypatch.setattr(ai, "generate_edited_trip_plan", generator)
    result = await evaluation.evaluate_case(suite.cases[0], suite)
    assert result["status"] == "quality_failed"
    assert result["failed_step"] == 1
    assert not result["checks"]["step:1:rest_changes"]
    assert len(result["steps"]) == 1
    analyzer.assert_awaited_once()
    generator.assert_not_awaited()


@pytest.mark.parametrize("failure", [ai.AIServiceError, RuntimeError])
@pytest.mark.asyncio
async def test_second_step_failure_keeps_first_actual_result_and_private_error_hidden(
    suite, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    analyzer = AsyncMock(
        side_effect=[
            TripEditAnalysis(
                period_actions=[],
                requires_regeneration=True,
                regeneration_periods=[
                    {"day": None, "period": period}
                    for period in ("morning", "afternoon", "evening")
                ],
                supported=True,
                interests_changed=False,
                interests=None,
                must_visit_places_changed=False,
                must_visit_places=suite.cases[0].preferences.must_visit_places,
                rest_period_changes=[
                    RestPeriodChange(day=None, period="evening", rest=True)
                ],
            ),
            failure("sentinel-private-error"),
        ]
    )
    generated = suite.plans["tokyo_1_days"].model_copy(deep=True)
    generated.days[0].evening = ["Спокойный отдых."]
    generator = AsyncMock(return_value=generated)
    monkeypatch.setattr(ai, "analyze_trip_edit", analyzer)
    monkeypatch.setattr(ai, "generate_edited_trip_plan", generator)
    level = ai.logger.level
    result = await evaluation.evaluate_case(suite.cases[0], suite)
    assert result["status"] == (
        "service_error" if failure is ai.AIServiceError else "runner_error"
    )
    assert result["failed_step"] == 2
    assert len(result["steps"]) == 2
    assert result["steps"][1]["input_plan"] == result["steps"][0]["plan"]
    generator.assert_awaited_once()
    assert "sentinel-private-error" not in json.dumps(result)
    assert ai.logger.level == level


@pytest.mark.parametrize("lost_field", ["rest_periods", "must_visit_places"])
@pytest.mark.asyncio
async def test_merged_preferences_are_checked_independently_before_generation(
    suite, monkeypatch: pytest.MonkeyPatch, lost_field: str
) -> None:
    """Верный разбор не скрывает потерю отдыха или обязательных мест при merge."""

    case = suite.cases[0]
    analysis = TripEditAnalysis(
        period_actions=[],
        requires_regeneration=True,
        regeneration_periods=[
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
        supported=True,
        interests_changed=False,
        interests=None,
        must_visit_places_changed=False,
        must_visit_places=case.preferences.must_visit_places,
        rest_period_changes=[RestPeriodChange(day=None, period="evening", rest=True)],
    )
    broken = case.preferences.model_copy(deep=True)
    broken.rest_periods = case.steps[0].expectations.rest_periods.copy()
    setattr(broken, lost_field, [])
    monkeypatch.setattr(ai, "analyze_trip_edit", AsyncMock(return_value=analysis))
    monkeypatch.setattr(evaluation, "merge_edit_preferences", lambda *_: broken)
    generator = AsyncMock()
    monkeypatch.setattr(ai, "generate_edited_trip_plan", generator)

    result = await evaluation.evaluate_case(case, suite)

    criterion = (
        "rest_preferences" if lost_field == "rest_periods" else "required_preferences"
    )
    assert result["status"] == "quality_failed"
    assert result["checks"]["step:1:rest_changes"]
    assert not result["checks"][f"step:1:{criterion}"]
    assert result["failed_step"] == 1
    generator.assert_not_awaited()


def test_period_checks_detect_wrong_rest_day_and_preserved_visit_loss(suite) -> None:
    case = suite.cases[1]
    expected = case.steps[0].expectations
    plan = suite.plans[case.current_plan].model_copy(deep=True)
    plan.days[0].evening = ["Спокойный отдых."]
    plan.days[1].evening = ["Спокойный отдых."]
    plan.days[0].morning = ["Посещение музея."]
    checks = evaluation.assess_edit_periods(
        plan, case, expected, suite.contexts[case.context]
    )
    assert checks["quiet_rest:2:evening"]
    assert not checks["unexpected_quiet_rest_absent"]
    assert not checks["general_activity:1:evening"]
    assert not checks["preserved:1:morning"]


def test_equivalent_day_scopes_and_specific_exception_are_compared_by_meaning() -> None:
    def change(day, enabled):
        return RestPeriodChange(day=day, period="evening", rest=enabled)

    assert evaluation.expanded_rest_changes([change(None, True)], 1) == (
        evaluation.expanded_rest_changes([change(1, True)], 1)
    )
    assert evaluation.expanded_rest_changes(
        [change(2, False), change(None, True)], 3
    ) == {
        (1, "evening"): True,
        (2, "evening"): False,
        (3, "evening"): True,
    }


def test_new_cli_is_offline_until_live_is_explicit(monkeypatch, capsys) -> None:
    settings = AsyncMock()
    analyzer = AsyncMock()
    monkeypatch.setattr(evaluation, "get_settings", settings)
    monkeypatch.setattr(ai, "analyze_trip_edit", analyzer)
    assert evaluation.main(["--suite", "edits"]) == 0
    assert "tokyo_rest_edit_cycle" in capsys.readouterr().out
    settings.assert_not_called()
    analyzer.assert_not_awaited()


def test_live_edit_cli_reports_all_turns_and_interval(
    monkeypatch, tmp_path: Path
) -> None:
    requests = mock_provider(monkeypatch)
    pauses = []

    async def sleep(interval):
        pauses.append(interval)

    monkeypatch.setattr(evaluation.asyncio, "sleep", sleep)
    path = tmp_path / "edit-report.json"
    assert (
        evaluation.main(
            [
                "--suite",
                "edits",
                "--live",
                "--case",
                "all",
                "--repeat",
                "1",
                "--interval-seconds",
                "65",
                "--output",
                str(path),
            ]
        )
        == 0
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["completed"]
    assert report["planned_runs"] == 3 and report["planned_messages"] == 7
    assert report["summary"] == {"passed": 3}
    assert len(requests) == 14
    assert pauses == [65] * 6
    assert [len(row["steps"]) for row in report["results"]] == [3, 3, 1]
    assert "fixture-secret" not in path.read_text(encoding="utf-8")


def test_quota_diagnostics_accept_only_numbers_and_drop_private_fields() -> None:
    collector = evaluation.CallCollector()
    event = {
        "event": "llm_call",
        "outcome": "http_error",
        "request_body_bytes": 1000,
        "provider_token_limit": 8000,
        "provider_tokens_remaining": "sentinel-secret",
        "retry_after_ms": True,
        "message": "sentinel-secret",
        "request_id": "sentinel-secret",
    }
    record = logging.LogRecord(
        "test", logging.WARNING, "", 0, "LLM call | %s", (json.dumps(event),), None
    )
    collector.emit(record)
    assert collector.calls[0]["request_body_bytes"] == 1000
    assert collector.calls[0]["provider_token_limit"] == 8000
    assert "provider_tokens_remaining" not in collector.calls[0]
    assert "retry_after_ms" not in collector.calls[0]
    assert "sentinel-secret" not in json.dumps(collector.calls)


@pytest.mark.asyncio
async def test_scoped_replacement_preserves_focus_and_general_periods(
    suite, monkeypatch
):
    requests = mock_provider(monkeypatch)
    case = suite.cases[2]
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed", result
    assert len(requests) == len(result["calls"]) == 2
    assert not result["retry_used"]
    turn = result["steps"][0]
    assert turn["analysis"]["period_actions"] == []
    assert turn["checks"]["regeneration_scope"]
    assert turn["checks"]["preserved_focus:1:afternoon"]
    assert turn["checks"]["replacement:2:afternoon"]
    assert turn["plan"]["days"][1]["afternoon"][0].startswith("Тестовый музей 5:")
    for old, new in zip(turn["input_plan"]["days"], turn["plan"]["days"], strict=True):
        assert old["evening"] == new["evening"]
    assert {
        (item["day"], item["period"]) for item in requests[1]["preserved_periods"]
    } == {
        (1, "morning"),
        (1, "afternoon"),
        (1, "evening"),
        (2, "morning"),
        (2, "evening"),
    }


def test_replacement_eval_detects_unchanged_target_and_changed_preserved_focus(suite):
    case = suite.cases[2]
    plan = suite.plans[case.current_plan].model_copy(deep=True)
    plan.days[0].afternoon = ["Mitsuo Aida Museum: осмотреть архитектурный объект."]
    checks = evaluation.assess_edit_periods(
        plan, case, case.steps[0].expectations, suite.contexts[case.context]
    )
    assert checks["preserved:1:afternoon"]
    assert not checks["preserved_focus:1:afternoon"]
    assert not checks["replacement:2:afternoon"]


@pytest.mark.parametrize(
    "defect", ["scope_day", "scope_duplicate", "unknown_id", "preserved_conflict"]
)
def test_invalid_scoped_replacement_corpus_is_rejected(suite, defect):
    data = suite.model_dump(mode="json")
    case = data["cases"][2]
    expected = case["steps"][0]["expectations"]
    if defect == "scope_day":
        expected["regeneration_periods"][0]["day"] = 3
    elif defect == "scope_duplicate":
        expected["regeneration_periods"] *= 2
    elif defect == "unknown_id":
        expected["replacement_visits"][0]["place_ids"] = ["unknown"]
    else:
        expected["replacement_visits"][0]["period"] = "morning"
    with pytest.raises(ValueError):
        evaluation.EditEvaluationSuite.model_validate_json(json.dumps(data))
