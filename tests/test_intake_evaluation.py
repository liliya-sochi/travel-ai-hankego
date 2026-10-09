"""Проверяет оценку intake без настоящей сети и расхода токенов."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.schemas.trip import TripDraft, TripIntakeResponse
from app.services import ai, trip_intake
from scripts import evaluate_routes as evaluation


@pytest.fixture
def suite() -> evaluation.IntakeEvaluationSuite:
    """Использует тот же корпус, который запускается вручную."""

    return evaluation.load_intake_suite()


def case_by_id(
    suite: evaluation.IntakeEvaluationSuite, case_id: str
) -> evaluation.IntakeEvaluationCase:
    return next(case for case in suite.cases if case.id == case_id)


def response_for(expected: evaluation.IntakeExpectations) -> TripIntakeResponse:
    """Создаёт ответ для проверки раннера, а не для тестирования LLM."""

    return TripIntakeResponse(
        intent=expected.intent,
        draft=expected.draft.model_copy(deep=True),
        missing_required_fields=expected.missing_required_fields.copy(),
        ready_to_generate=expected.ready_to_generate,
        next_question=(
            trip_intake.NEXT_QUESTIONS[expected.next_question_for]
            if expected.next_question_for is not None
            else None
        ),
    )


def test_corpus_covers_complete_requests_and_dialogues(
    suite: evaluation.IntakeEvaluationSuite,
) -> None:
    assert len(suite.cases) == 8
    assert sum(len(case.steps) for case in suite.cases) == 11
    assert {case.steps[-1].expectations.intent for case in suite.cases} == {
        "plan_trip",
        "cancel",
        "show_trips",
        "unknown",
    }


@pytest.mark.parametrize(
    "defect",
    ["duplicate", "long_message", "empty_term", "wrong_missing", "after_ready"],
)
def test_invalid_corpus_is_rejected_before_live_calls(
    suite: evaluation.IntakeEvaluationSuite, defect: str
) -> None:
    data = suite.model_dump(mode="json")
    case = data["cases"][0]
    expected = case["steps"][0]["expectations"]
    if defect == "duplicate":
        data["cases"].append(case)
    elif defect == "long_message":
        case["steps"][0]["user_message"] = "x" * 2001
    elif defect == "empty_term":
        expected["interests_contains"] = [" "]
    elif defect == "wrong_missing":
        expected["missing_required_fields"] = ["destination"]
    else:
        case["steps"].append(case["steps"][0])
    with pytest.raises(ValueError):
        evaluation.IntakeEvaluationSuite.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    ("field", "value"),
    [("budget", "999999"), ("travel_period", "Лето"), ("duration_days", 30)],
)
def test_invented_or_changed_facts_fail_quality_checks(
    suite: evaluation.IntakeEvaluationSuite, field: str, value: str | int
) -> None:
    expected = case_by_id(suite, "intake_tokyo_required").steps[0].expectations
    response = response_for(expected)
    setattr(response.draft, field, value)
    checks = evaluation.assess_intake(response, expected)
    assert not checks[f"draft:{field}"]


def test_readiness_and_question_are_checked_independently(
    suite: evaluation.IntakeEvaluationSuite,
) -> None:
    expected = case_by_id(suite, "intake_dialogue_completion").steps[0].expectations
    response = response_for(expected)
    response.ready_to_generate = True
    response.next_question = trip_intake.NEXT_QUESTIONS["duration_days"]
    response.missing_required_fields = ["duration_days"]
    checks = evaluation.assess_intake(response, expected)
    assert not checks["ready_to_generate"]
    assert not checks["next_question"]
    assert not checks["missing_required_fields"]


def test_required_places_ignore_order_but_not_loss_or_duplicates(
    suite: evaluation.IntakeEvaluationSuite,
) -> None:
    expected = (
        case_by_id(suite, "intake_correct_and_add_required").steps[1].expectations
    )
    response = response_for(expected)
    response.draft.must_visit_places.reverse()
    assert evaluation.assess_intake(response, expected)["draft:must_visit_places"]
    response.draft.must_visit_places.pop()
    assert not evaluation.assess_intake(response, expected)["draft:must_visit_places"]
    response.draft.must_visit_places = expected.draft.must_visit_places * 2
    assert not evaluation.assess_intake(response, expected)["draft:must_visit_places"]


def test_replacement_accepts_wording_but_detects_old_interests(
    suite: evaluation.IntakeEvaluationSuite,
) -> None:
    expected = (
        case_by_id(suite, "intake_correct_and_add_required").steps[0].expectations
    )
    response = response_for(expected)
    response.draft.interests = "  Посещение МУЗЕЕВ  "
    assert all(evaluation.assess_intake(response, expected).values())
    response.draft.interests = "Музеи и парки"
    assert not evaluation.assess_intake(response, expected)["draft:interests_excludes"]
    response.draft.interests = None
    assert not evaluation.assess_intake(response, expected)["draft:interests"]


@pytest.mark.asyncio
async def test_dialogue_passes_actual_state_and_paces_every_message(
    suite: evaluation.IntakeEvaluationSuite,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = case_by_id(suite, "intake_dialogue_completion")
    seen: list[TripDraft] = []

    async def fake_service(
        *, user_message: str, draft: TripDraft
    ) -> TripIntakeResponse:
        index = len(seen) % len(case.steps)
        assert user_message == case.steps[index].user_message
        seen.append(draft.model_copy(deep=True))
        response = response_for(case.steps[index].expectations)
        response.draft.interests = "Прогулки по паркам"
        return response

    sleep = AsyncMock()
    monkeypatch.setattr(trip_intake, "process_trip_message", fake_service)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    report = {"completed": False, "results": []}
    await evaluation.run_suite(
        [case],
        suite,
        repeats=2,
        interval=65,
        path=tmp_path / "report.json",
        report=report,
    )
    assert report["summary"] == {"passed": 2}
    assert seen[0] == seen[3] == TripDraft()
    assert seen[1].interests == seen[2].interests == "Прогулки по паркам"
    assert case.initial_draft == TripDraft()
    assert sleep.await_count == 5
    assert all(call.args == (65,) for call in sleep.await_args_list)


@pytest.mark.asyncio
async def test_failed_step_stops_without_followup_or_outer_retry(
    suite: evaluation.IntakeEvaluationSuite, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = case_by_id(suite, "intake_correct_and_add_required")
    response = response_for(case.steps[0].expectations)
    response.draft.must_visit_places = []
    service = AsyncMock(return_value=response)
    monkeypatch.setattr(trip_intake, "process_trip_message", service)
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "quality_failed"
    assert result["failed_step"] == 1
    assert not result["checks"]["step:1:draft:must_visit_places"]
    assert len(result["steps"]) == 1
    service.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "status"),
    [(ai.AIServiceError, "service_error"), (RuntimeError, "runner_error")],
)
async def test_errors_keep_attempted_turn_without_exception_text(
    suite: evaluation.IntakeEvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    status: str,
) -> None:
    case = case_by_id(suite, "intake_dialogue_completion")
    monkeypatch.setattr(
        trip_intake,
        "process_trip_message",
        AsyncMock(side_effect=error_type("sentinel-secret")),
    )
    old_level = ai.logger.level
    old_handlers = ai.logger.handlers.copy()
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == status
    assert len(result["steps"]) == 1
    assert "response" not in result["steps"][0]
    assert "sentinel-secret" not in json.dumps(result)
    assert ai.logger.level == old_level
    assert ai.logger.handlers == old_handlers


def test_offline_intake_does_not_read_settings_or_call_services(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden_settings() -> None:
        pytest.fail("Просмотр корпуса не должен читать ключи.")

    monkeypatch.setattr(evaluation, "get_settings", forbidden_settings)
    intake = AsyncMock()
    generator = AsyncMock()
    monkeypatch.setattr(trip_intake, "process_trip_message", intake)
    monkeypatch.setattr(ai, "generate_trip_plan", generator)
    assert evaluation.main(["--suite", "intake"]) == 0
    assert "intake_dialogue_completion" in capsys.readouterr().out
    intake.assert_not_awaited()
    generator.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments",
    [["--live"], ["--live", "--case", "tokyo_required_parks"], ["--repeat", "0"]],
)
def test_invalid_intake_cli_stops_before_settings(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    settings = AsyncMock()
    monkeypatch.setattr(evaluation, "get_settings", settings)
    with pytest.raises(SystemExit) as error:
        evaluation.main(["--suite", "intake", *arguments])
    assert error.value.code == 2
    settings.assert_not_called()


@pytest.mark.parametrize("wrong_duration", [False, True])
def test_live_intake_cli_writes_independent_criteria_and_counts(
    suite: evaluation.IntakeEvaluationSuite,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrong_duration: bool,
) -> None:
    case = case_by_id(suite, "intake_instruction_injection")
    response = response_for(case.steps[0].expectations)
    if wrong_duration:
        response.draft.duration_days = 30
    monkeypatch.setattr(
        evaluation,
        "get_settings",
        lambda: SimpleNamespace(
            llm_model="fixture-model", llm_analysis_model="fixture-analysis-model"
        ),
    )
    monkeypatch.setattr(
        trip_intake, "process_trip_message", AsyncMock(return_value=response)
    )
    generator = AsyncMock()
    monkeypatch.setattr(ai, "generate_trip_plan", generator)
    path = tmp_path / "intake.json"
    code = evaluation.main(
        ["--suite", "intake", "--live", "--case", case.id, "--output", str(path)]
    )
    assert code == (1 if wrong_duration else 0)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["suite"] == "intake"
    assert report["completed"]
    assert report["planned_messages"] == report["planned_runs"] == 1
    assert (
        report["results"][0]["steps"][0]["expectations"]["draft"]["duration_days"] == 1
    )
    assert report["summary"] == {"quality_failed" if wrong_duration else "passed": 1}
    generator.assert_not_awaited()


class DummyAsyncClient:
    """Не создаёт сетевой клиент для тестов настоящего intake-сервиса."""

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def provider_response(content: str) -> ai.LLMProviderResponse:
    """Подменяет только внешние байты, сохраняя настоящий разбор и merge."""

    return ai.LLMProviderResponse(
        data={
            "model": "fixture-analysis-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        },
        duration_ms=1,
        header_request_id="fixture-request",
    )


def mock_provider(
    monkeypatch: pytest.MonkeyPatch, responses: list[str]
) -> list[dict[str, Any]]:
    """Оставляет настоящими процессинг, Structured Output, retry и логи."""

    seen: list[dict[str, Any]] = []

    async def request(**arguments: Any) -> ai.LLMProviderResponse:
        payload = arguments["payload"]
        assert payload["model"] == "fixture-analysis-model"
        assert payload["response_format"]["json_schema"]["strict"]
        seen.append(json.loads(payload["messages"][1]["content"]))
        return provider_response(responses.pop(0))

    monkeypatch.setattr(
        ai,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.invalid/v1",
            llm_api_key="sentinel-secret",
            llm_model="fixture-model",
            llm_analysis_model="fixture-analysis-model",
        ),
    )
    monkeypatch.setattr(ai.httpx, "AsyncClient", DummyAsyncClient)
    monkeypatch.setattr(ai, "_request_model", request)
    return seen


def extraction(**fields: object) -> str:
    """Все nullable-поля присутствуют в strict ответе провайдера."""

    data = dict(
        intent="plan_trip",
        destination=None,
        duration_days=None,
        travel_period=None,
        budget=None,
        interests=None,
        must_visit_places=None,
    )
    return json.dumps(data | fields, ensure_ascii=False)


@pytest.mark.asyncio
async def test_real_service_merges_short_answers_and_preserves_actual_interests(
    suite: evaluation.IntakeEvaluationSuite, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = case_by_id(suite, "intake_dialogue_completion")
    seen = mock_provider(
        monkeypatch,
        [
            extraction(interests="Прогулки по паркам"),
            extraction(destination="Токио"),
            extraction(duration_days=7),
        ],
    )
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed"
    assert seen[1]["current_draft"]["interests"] == "Прогулки по паркам"
    assert seen[2]["current_draft"]["destination"] == "Токио"
    assert seen[2]["current_draft"]["interests"] == "Прогулки по паркам"
    assert result["known_total_tokens"] == 90
    assert result["usage_complete"]
    assert len(result["calls"]) == 3
    assert not result["retry_used"]
    assert "sentinel-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_real_service_preserves_optional_facts_and_adds_required_place(
    suite: evaluation.IntakeEvaluationSuite, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = case_by_id(suite, "intake_correct_and_add_required")
    mock_provider(
        monkeypatch,
        [
            extraction(interests="Посещение музеев"),
            extraction(
                duration_days=2,
                must_visit_places=[
                    "Mitsubishi Ichigokan Museum",
                    "Art Aquarium Museum GINZA",
                ],
            ),
        ],
    )
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == "passed"
    final = result["steps"][-1]["response"]["draft"]
    assert final["budget"] == case.initial_draft.budget
    assert final["travel_period"] == case.initial_draft.travel_period
    assert final["interests"] == "Посещение музеев"
    assert len(final["must_visit_places"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_after_retry", [False, True])
async def test_real_validation_retry_is_counted_without_extra_batch_retry(
    suite: evaluation.IntakeEvaluationSuite,
    monkeypatch: pytest.MonkeyPatch,
    valid_after_retry: bool,
) -> None:
    case = case_by_id(suite, "intake_tokyo_required")
    valid = extraction(
        destination="Токио",
        duration_days=1,
        interests="Парки",
        must_visit_places=["Mitsubishi Ichigokan Museum"],
    )
    seen = mock_provider(
        monkeypatch,
        [
            '{"intent":"plan_trip"}',
            valid if valid_after_retry else '{"intent":"plan_trip"}',
        ],
    )
    result = await evaluation.evaluate_case(case, suite)
    assert result["status"] == ("passed" if valid_after_retry else "service_error")
    assert len(seen) == len(result["calls"]) == 2
    assert result["retry_used"]
    assert result["calls"][0]["outcome"] == "invalid_output"
    assert result["known_total_tokens"] == 60
