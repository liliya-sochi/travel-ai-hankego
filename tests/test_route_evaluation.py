"""Проверяет оценщик без настоящих LLM-запросов и расхода токенов."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.trip import DayPlan, TripEditAnalysis, TripPlanResponse
from app.services import ai
from scripts import evaluate_routes as evaluation


@pytest.fixture
def suite() -> evaluation.EvaluationSuite:
    """Читает тот же корпус, который используется при ручной оценке."""

    return evaluation.load_suite()


def case_by_id(
    suite: evaluation.EvaluationSuite, case_id: str
) -> evaluation.EvaluationCase:
    return next(case for case in suite.cases if case.id == case_id)


def istanbul_plan(
    suite: evaluation.EvaluationSuite, second_afternoon: str
) -> TripPlanResponse:
    """Сохраняет интересы и число мест, меняя только географию второго дня."""

    by_id = {
        place.source_place_id: place for place in suite.contexts["istanbul"].places
    }

    def visit(place_id: str) -> list[str]:
        return [f"{by_id[place_id].display_name}: посетить место."]

    return TripPlanResponse(
        destination="Стамбул",
        duration_days=2,
        summary="История и архитектура.",
        days=[
            DayPlan(
                day=1,
                title="История и архитектура",
                morning=visit("eval-ist-irene"),
                afternoon=visit("eval-ist-orient"),
                evening=["Спокойная прогулка."],
            ),
            DayPlan(
                day=2,
                title="Музеи",
                morning=visit("eval-ist-military"),
                afternoon=visit(second_afternoon),
                evening=["Отдых."],
            ),
        ],
        practical_tips=[],
    )


def test_corpus_has_expected_cases_and_accepts_json_datetimes(
    suite: evaluation.EvaluationSuite,
) -> None:
    assert [case.id for case in suite.cases] == [
        "istanbul_history_architecture",
        "tokyo_required_parks",
        "tokyo_museum_edit",
        "tokyo_duration_edit_rejected",
    ]
    assert suite.contexts["istanbul"].fetched_at.tzinfo is not None


def test_duplicate_ids_are_rejected(suite: evaluation.EvaluationSuite) -> None:
    data = suite.model_dump(mode="json")
    data["cases"].append(data["cases"][0])
    with pytest.raises(ValueError, match="уникальными"):
        evaluation.EvaluationSuite.model_validate_json(json.dumps(data))


def test_unknown_expected_place_is_a_corpus_error(
    suite: evaluation.EvaluationSuite,
) -> None:
    data = suite.model_dump(mode="json")
    data["cases"][0]["expectations"]["required_place_ids"] = ["unknown-place"]
    with pytest.raises(ValueError, match="отсутствует"):
        evaluation.EvaluationSuite.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    ("second_afternoon", "compact"),
    [("eval-ist-ataturk", True), ("eval-ist-islamic", False)],
)
def test_compactness_measures_the_returned_plan(
    suite: evaluation.EvaluationSuite,
    second_afternoon: str,
    compact: bool,
) -> None:
    case = case_by_id(suite, "istanbul_history_architecture")
    assessment = evaluation.assess_plan(
        istanbul_plan(suite, second_afternoon), case, suite.contexts[case.context]
    )
    assert assessment["checks"]["compact_days"] is compact
    assert assessment["checks"]["interest:building.historic"]
    assert assessment["checks"]["interest:building.tourism"]
    assert (assessment["daily_max_distance_meters"][1] > 4_000) is not compact


def test_summary_mention_does_not_preserve_required_place(
    suite: evaluation.EvaluationSuite,
) -> None:
    case = case_by_id(suite, "tokyo_required_parks")
    plan = suite.plans["tokyo_parks"].model_copy(deep=True)
    plan.days[0].morning = ["Отдых."]
    result = evaluation.assess_plan(plan, case, suite.contexts[case.context])
    assert "Mitsubishi Ichigokan Museum" in plan.summary
    assert not result["checks"]["required_places"]
    assert not result["checks"]["places_per_day"]


def test_edit_expectations_are_not_taken_from_model_preferences(
    suite: evaluation.EvaluationSuite,
) -> None:
    case = case_by_id(suite, "tokyo_museum_edit")
    result = evaluation.assess_plan(
        suite.plans["tokyo_parks"], case, suite.contexts[case.context]
    )
    assert not result["checks"]["forbidden_categories_absent"]
    assert not result["checks"]["general_evening_only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [False, True])
async def test_rejected_edit_stops_after_analysis(
    suite: evaluation.EvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
    supported: bool,
) -> None:
    case = case_by_id(suite, "tokyo_duration_edit_rejected")
    analyzer = AsyncMock(
        return_value=TripEditAnalysis(
            supported=supported,
            interests_changed=False,
            interests=None,
            must_visit_places_changed=False,
            must_visit_places=case.preferences.must_visit_places,
        )
    )
    generator = AsyncMock()
    monkeypatch.setattr(ai, "analyze_trip_edit", analyzer)
    monkeypatch.setattr(ai, "generate_edited_trip_plan", generator)
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == ("quality_failed" if supported else "passed")
    assert result["checks"]["edit_rejected"] is not supported
    analyzer.assert_awaited_once()
    generator.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_error_is_separate_and_does_not_expose_error_text(
    suite: evaluation.EvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = case_by_id(suite, "tokyo_required_parks")
    monkeypatch.setattr(
        ai,
        "generate_trip_plan",
        AsyncMock(side_effect=ai.AIServiceError("sentinel-secret")),
    )
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "service_error"
    assert "plan" not in result
    assert not result["usage_complete"]
    assert "sentinel-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_accepted_distant_plan_is_a_quality_failure(
    suite: evaluation.EvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = case_by_id(suite, "istanbul_history_architecture")
    monkeypatch.setattr(
        ai,
        "generate_trip_plan",
        AsyncMock(return_value=istanbul_plan(suite, "eval-ist-islamic")),
    )
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "quality_failed"
    assert not result["checks"]["compact_days"]
    assert result["plan"]["duration_days"] == 2


def test_collector_uses_allowlist_and_ignores_other_logs() -> None:
    collector = evaluation.CallCollector()
    record = logging.LogRecord(
        "app.services.ai",
        logging.INFO,
        "",
        1,
        "LLM call | %s",
        (
            json.dumps(
                {
                    "outcome": "success",
                    "total_tokens": 123,
                    "prompt": "sentinel-secret",
                    "request_id": "private-id",
                }
            ),
        ),
        None,
    )
    collector.emit(record)
    collector.emit(
        logging.LogRecord(
            "app.services.ai", logging.WARNING, "", 1, "sentinel-secret", (), None
        )
    )
    assert collector.calls == [{"outcome": "success", "total_tokens": 123}]


@pytest.mark.parametrize("payload", ["invalid JSON", "[]", None])
def test_malformed_metadata_does_not_break_the_model_call(payload: str | None) -> None:
    collector = evaluation.CallCollector()
    collector.emit(
        logging.LogRecord(
            "app.services.ai", logging.INFO, "", 1, "LLM call | %s", (payload,), None
        )
    )
    assert collector.calls == []


def test_default_cli_does_not_load_settings_or_call_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden_settings() -> None:
        pytest.fail("Настройки не нужны для просмотра корпуса.")

    monkeypatch.setattr(evaluation, "get_settings", forbidden_settings)
    generator = AsyncMock()
    monkeypatch.setattr(ai, "generate_trip_plan", generator)
    assert evaluation.main([]) == 0
    assert "tokyo_museum_edit" in capsys.readouterr().out
    generator.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        ["--live"],
        ["--live", "--case", "unknown"],
        ["--repeat", "11"],
        ["--interval-seconds", "nan"],
    ],
)
def test_invalid_cli_stops_before_settings_and_network(
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    def forbidden_settings() -> None:
        pytest.fail("Недопустимый запуск не должен читать секреты.")

    monkeypatch.setattr(evaluation, "get_settings", forbidden_settings)
    with pytest.raises(SystemExit) as error:
        evaluation.main(args)
    assert error.value.code == 2


def test_existing_report_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "report.json"
    path.write_text("previous report", encoding="utf-8")
    settings = AsyncMock()
    monkeypatch.setattr(evaluation, "get_settings", settings)
    with pytest.raises(SystemExit):
        evaluation.main(["--live", "--case", "all", "--output", str(path)])
    assert path.read_text(encoding="utf-8") == "previous report"
    settings.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "exit_code", "status"),
    [
        (None, 0, "passed"),
        (ai.AIServiceError, 1, "service_error"),
        (RuntimeError, 2, "runner_error"),
    ],
)
def test_live_cli_saves_report_and_returns_meaningful_exit_code(
    suite: evaluation.EvaluationSuite,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception] | None,
    exit_code: int,
    status: str,
) -> None:
    path = tmp_path / "report.json"
    monkeypatch.setattr(
        evaluation,
        "get_settings",
        lambda: SimpleNamespace(
            llm_model="fixture-model",
            llm_analysis_model="fixture-analysis-model",
        ),
    )
    generator = AsyncMock(return_value=suite.plans["tokyo_parks"])
    if failure is not None:
        generator.side_effect = failure("sentinel-secret")
    monkeypatch.setattr(ai, "generate_trip_plan", generator)
    assert (
        evaluation.main(
            [
                "--live",
                "--case",
                "tokyo_required_parks",
                "--output",
                str(path),
            ]
        )
        == exit_code
    )
    report_text = path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["completed"]
    assert report["summary"] == {status: 1}
    assert report["planned_runs"] == 1
    assert "sentinel-secret" not in report_text
    assert "llm_api_key" not in report_text
    assert len(report["fixture_sha256"]) == 64
    assert not path.with_name(f"{path.name}.tmp").exists()


@pytest.mark.asyncio
async def test_batch_checkpoints_completed_runs(
    suite: evaluation.EvaluationSuite,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path: Path = tmp_path / "report.json"
    report = {"completed": False, "results": []}
    seen = []

    async def fake_evaluation(
        case: evaluation.EvaluationCase,
        current_suite: evaluation.EvaluationSuite,
    ) -> dict[str, object]:
        previous = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        assert not previous["completed"]
        assert len(previous["results"]) == len(seen)
        assert current_suite is suite
        seen.append(case.id)
        return {
            "status": "passed",
            "case_id": case.id,
            "calls": [],
            "retry_used": False,
        }

    monkeypatch.setattr(evaluation, "evaluate_case", fake_evaluation)
    await evaluation.run_suite(
        suite.cases[:2],
        suite,
        repeats=2,
        interval=0,
        path=path,
        report=report,
    )
    saved = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    assert saved["completed"]
    assert saved["summary"] == {"passed": 4}
    assert [row["repeat"] for row in saved["results"]] == [1, 1, 2, 2]
    assert seen == [case.id for case in suite.cases[:2]] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_retry", [False, True])
async def test_real_ai_validation_and_geography_are_included(
    suite: evaluation.EvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
    needs_retry: bool,
) -> None:
    # Сеть подменена на границе LLM; настройки прокси среды тесту не нужны.
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(variable, raising=False)
    case = case_by_id(suite, "istanbul_history_architecture")
    places = {
        place.source_place_id: place for place in suite.contexts[case.context].places
    }

    def activity(place_id: str, focus: str) -> dict[str, str | None]:
        return {
            "source_place_id": place_id,
            "place_name": places[place_id].name,
            "activity_focus": focus,
            "description": None,
        }

    general = {
        "source_place_id": None,
        "place_name": None,
        "activity_focus": None,
        "description": "Спокойная прогулка.",
    }
    raw = {
        "destination": "Стамбул",
        "duration_days": 2,
        "summary": "История и архитектура Стамбула.",
        "days": [
            {
                "day": 1,
                "title": "Непроверенный район",
                "morning": [activity("eval-ist-irene", "history")],
                "afternoon": [activity("eval-ist-orient", "architecture")],
                "evening": [general],
            },
            {
                "day": 2,
                "title": "Непроверенный район",
                "morning": [activity("eval-ist-military", "museum")],
                "afternoon": [activity("eval-ist-islamic", "museum")],
                "evening": [general],
            },
        ],
    }

    def response(payload: dict[str, object]) -> ai.LLMProviderResponse:
        return ai.LLMProviderResponse(
            data={
                "model": "fixture-model",
                "choices": [
                    {
                        "message": {"content": json.dumps(payload)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 100},
            },
            duration_ms=1,
            header_request_id="fixture-id",
        )

    responses = [response(raw)]
    if needs_retry:
        responses.insert(0, response({**raw, "destination": "Другой город"}))
    monkeypatch.setattr(
        ai,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.invalid",
            llm_api_key="test-key",
            llm_model="fixture-model",
        ),
    )
    requester = AsyncMock(side_effect=responses)
    monkeypatch.setattr(ai, "_request_model", requester)
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed", result
    assert "eval-ist-ataturk" in result["selected_place_ids"]
    assert "eval-ist-islamic" not in result["selected_place_ids"]
    assert result["retry_used"] is needs_retry
    assert len(result["calls"]) == (2 if needs_retry else 1)
    assert result["known_total_tokens"] == 100 * len(result["calls"])
    assert result["usage_complete"]
    assert result["plan"]["days"][1]["title"] == "Музеи"
    requester.assert_awaited()


@pytest.mark.asyncio
async def test_live_edit_path_updates_interests_and_uses_fixed_original_plan(
    suite: evaluation.EvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(variable, raising=False)
    case = case_by_id(suite, "tokyo_museum_edit")
    context = suite.contexts[case.context]
    places = {place.source_place_id: place for place in context.places}

    def visit(place_id: str) -> dict[str, str | None]:
        return {
            "source_place_id": place_id,
            "place_name": places[place_id].name,
            "activity_focus": "museum",
            "description": None,
        }

    analysis = {
        "supported": True,
        "interests_changed": True,
        "interests": "Музеи, вечером спокойный отдых",
        "must_visit_places_changed": False,
        "must_visit_places": case.preferences.must_visit_places,
    }
    raw_plan = {
        "destination": "Токио",
        "duration_days": 1,
        "summary": "Два музея и спокойный отдых.",
        "days": [
            {
                "day": 1,
                "title": "Непроверенный район",
                "morning": [visit("eval-tokyo-mitsubishi")],
                "afternoon": [visit("eval-tokyo-aquarium")],
                "evening": [
                    {
                        "source_place_id": None,
                        "place_name": None,
                        "activity_focus": None,
                        "description": "Спокойный отдых в отеле.",
                    }
                ],
            }
        ],
    }
    responses = [
        ai.LLMProviderResponse(
            data={
                "choices": [
                    {
                        "message": {"content": json.dumps(payload)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 100},
            },
            duration_ms=1,
            header_request_id="fixture-id",
        )
        for payload in [analysis, raw_plan]
    ]
    monkeypatch.setattr(
        ai,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.invalid",
            llm_api_key="test-key",
            llm_model="fixture-model",
            llm_analysis_model="fixture-analysis-model",
        ),
    )
    requester = AsyncMock(side_effect=responses)
    monkeypatch.setattr(ai, "_request_model", requester)
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed", result
    assert result["updated_preferences"]["interests"] == analysis["interests"]
    assert case.preferences.interests == "Парки"
    assert "eval-tokyo-mitsubishi" in result["selected_place_ids"]
    assert result["checks"]["general_evening_only"]
    assert len(result["calls"]) == 2
    assert not result["retry_used"]
    generation_payload = requester.call_args_list[1].kwargs["payload"]
    generation_input = json.loads(generation_payload["messages"][1]["content"])
    assert generation_input["current_trip_plan"] == suite.plans[
        case.current_plan
    ].model_dump(mode="json")
    assert generation_input["trip_preferences"]["interests"] == analysis["interests"]
