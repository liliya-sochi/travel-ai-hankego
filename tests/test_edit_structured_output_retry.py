"""Provider 400 по structured output расходует общий бюджет анализа, не расширяя его."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import ai
from scripts import evaluate_routes as evaluation
from tests.test_period_interest_scope import scoped_reply

PRIVATE = "PRIVATE_PROVIDER_BODY_OR_KEY"


def schema_error(code="json_validate_failed"):
    return (400, {"error": {"code": code, "message": PRIVATE}})


def replay(monkeypatch, responses):
    queue = list(responses)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        assert queue, "Незапланированный дополнительный вызов"
        reply = queue.pop(0)
        if isinstance(reply, tuple):
            status, body = reply
            return httpx.Response(status, json=body, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        ai.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    settings = SimpleNamespace(
        llm_base_url="https://example.invalid/v1",
        llm_api_key=PRIVATE,
        llm_analysis_model="fixture-analysis",
        llm_model="fixture-generation",
    )
    monkeypatch.setattr(ai, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    sleep = AsyncMock()
    monkeypatch.setattr(ai.asyncio, "sleep", sleep)
    return requests, queue, sleep


async def analyze():
    suite = evaluation.load_edit_suite()
    case = suite.cases[0]
    return await ai.analyze_trip_edit(
        current_preferences=case.preferences,
        current_plan=suite.plans[case.current_plan],
        instruction=case.steps[0].user_message,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["json_validate_failed", "json_schema_validation_failed"]
)
async def test_known_provider_schema_error_recovers_with_same_strict_schema(
    monkeypatch, caplog, code
):
    requests, queue, sleep = replay(
        monkeypatch, [schema_error(code), scoped_reply(rest=True)]
    )
    with caplog.at_level(logging.INFO, logger=ai.__name__):
        parsed = await analyze()
    assert parsed.rest_period_changes[0].rest
    assert not parsed.interests_changed and not parsed.requires_regeneration
    assert len(requests) == 2 and not queue
    assert requests[0]["response_format"] == requests[1]["response_format"]
    assert requests[0]["response_format"]["json_schema"]["strict"]
    assert requests[1]["messages"][-1] == {
        "role": "user",
        "content": ai.EDIT_ANALYSIS_RETRY_PROMPT,
    }
    sleep.assert_awaited_once_with(ai.STRUCTURED_OUTPUT_RETRY_DELAY_SECONDS)
    assert PRIVATE not in caplog.text
    events = [
        json.loads(r.message.split(" | ", 1)[1])
        for r in caplog.records
        if r.message.startswith("LLM retry | ")
    ]
    assert len(events) == 1 and events[0]["reason"] == "structured_output_validation"


@pytest.mark.asyncio
async def test_repeated_structured_failure_stops_at_existing_analysis_limit(
    monkeypatch,
):
    requests, queue, sleep = replay(monkeypatch, [schema_error(), schema_error()])
    with pytest.raises(ai.AIProviderStructuredOutputError):
        await analyze()
    assert len(requests) == ai.MAX_SEMANTIC_ATTEMPTS == 2 and not queue
    sleep.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_model"),
        (400, "context_length_exceeded"),
        (400, "unknown_error"),
        (403, "json_validate_failed"),
        (413, "json_validate_failed"),
    ],
)
async def test_other_http_errors_are_not_retried(monkeypatch, status, code):
    requests, queue, sleep = replay(
        monkeypatch, [(status, {"error": {"code": code, "message": PRIVATE}})]
    )
    with pytest.raises(ai.AIServiceError):
        await analyze()
    assert len(requests) == 1 and not queue
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_first", [True, False])
async def test_provider_and_semantic_failures_share_two_attempt_budget(
    monkeypatch, provider_first
):
    bad = scoped_reply(rest=True)
    bad.update(interests_changed=True, interests="Музеи; отдых")
    replies = [schema_error(), bad] if provider_first else [bad, schema_error()]
    requests, queue, _ = replay(monkeypatch, replies)
    with pytest.raises(ai.AIServiceError):
        await analyze()
    assert len(requests) == 2 and not queue


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_provider_rate_limit_budget_stays_bounded_after_schema_retry(
    monkeypatch, recover
):
    limited = (429, {"error": {"message": PRIVATE}})
    requests, queue, sleep = replay(
        monkeypatch,
        [schema_error(), limited, scoped_reply(rest=True) if recover else limited],
    )
    if recover:
        assert (await analyze()).rest_period_changes[0].rest
    else:
        with pytest.raises(ai.AIProviderRateLimitError):
            await analyze()
    assert len(requests) == 3 and not queue
    assert sleep.await_count == 2


def test_live_report_records_failure_then_success_without_generation(
    monkeypatch, tmp_path
):
    requests, queue, _ = replay(monkeypatch, [schema_error(), scoped_reply(rest=True)])
    suite = evaluation.load_edit_suite()
    case = suite.cases[0]
    case.steps = case.steps[:1]
    monkeypatch.setattr(evaluation, "load_edit_suite", lambda: suite)
    output = tmp_path / "report.json"
    assert (
        evaluation.main(
            [
                "--suite",
                "edits",
                "--live",
                "--case",
                case.id,
                "--repeat",
                "1",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    result = report["results"][0]
    assert result["status"] == "passed" and result["retry_used"]
    assert [c["attempt"] for c in result["calls"]] == [1, 2]
    assert [c["outcome"] for c in result["calls"]] == ["http_error", "success"]
    assert all(c["model"] == "fixture-analysis" for c in result["calls"])
    assert result["calls"][0]["provider_error_code"] == "structured_output_validation"
    assert len(requests) == 2 and not queue
    assert PRIVATE not in output.read_text()
