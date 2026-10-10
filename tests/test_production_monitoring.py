"""Проверки сводки и мониторинга без Docker, сервера и отправки сообщений."""

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import trip as trip_api
from app.main import app
from app.schemas.trip import TripIntakeResponse
from app.services.ai import AIServiceError, LLMResponseMetadata, _log_llm_call
from scripts.report_production import latency_summary, summarize

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
PRIVATE = "PRIVATE_REQUEST_CONTENT_DO_NOT_EXPORT"


def log_line(
    message: str, *, at: datetime = NOW, logger: str = "app.services.ai"
) -> str:
    """Воспроизводит формат приложения с префиксом Docker Compose."""

    return (
        f"api-1  | {at:%Y-%m-%d %H:%M:%S} | INFO | {logger} | "
        f"correlation_id=private-correlation-id | {message}\n"
    )


def call_line(**changes: object) -> str:
    """Создаёт событие вызова с полным usage."""

    event = {
        "event": "llm_call",
        "model": "openai/gpt-oss-120b",
        "outcome": "success",
        "attempt": 1,
        "provider_attempt": 1,
        "duration_ms": 100,
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        **changes,
    }
    return log_line(f"LLM call | {json.dumps(event)}")


def http_line(status: int = 201, operation: str = "trip-plan", **kwargs: Any) -> str:
    """Создаёт итоговую запись запроса, а не access log Uvicorn."""

    return log_line(
        f"HTTP request completed | method=POST | path=/api/v1/{operation} | "
        f"status={status} | duration_ms=123.45",
        logger="app.main",
        **kwargs,
    )


def test_recovered_attempt_is_not_a_failed_request() -> None:
    """Повтор модели увеличивает расходы, но успешный API остаётся успешным."""

    report = summarize(
        [
            call_line(outcome="rate_limited", total_tokens=None),
            log_line(
                'LLM retry | {"event":"llm_retry","reason":"rate_limit","delay_ms":10000}'
            ),
            call_line(provider_attempt=2, duration_ms=300),
            http_line(),
            'api-1 | INFO: 127.0.0.1 - "POST /api/v1/trip-plan HTTP/1.1" 201\n',
        ],
        now=NOW,
        minimum_requests=1,
    )
    assert report["check"]["status"] == "healthy"
    assert report["api"]["trip-plan"]["requests"] == 1
    assert report["api"]["trip-plan"]["server_errors"] == 0
    assert report["llm"]["calls"] == 2
    assert report["llm"]["retry_calls"] == 1
    assert report["llm"]["outcomes"] == {"rate_limited": 1, "success": 1}
    assert report["scheduled_retries"] == {"rate_limit": 1}
    assert report["llm"]["usage"] == {
        "calls_with_complete_usage": 1,
        "calls_with_missing_usage": 1,
        "known_prompt_tokens": 20,
        "known_completion_tokens": 40,
        "known_total_tokens": 30,
    }


def test_each_operation_has_its_own_availability_threshold() -> None:
    """Успешный intake и ожидаемые 4xx не скрывают сбои генерации."""

    lines = [http_line(200, "trip-intake")] * 100
    lines += [http_line(503)] * 2 + [http_line(201)] * 2
    lines += [http_line(422), http_line(409), http_line(429)] * 10
    lines += [
        log_line(
            "HTTP request failed | method=POST | path=/api/v1/trip-plan | duration_ms=2500",
            logger="app.main",
        )
    ]
    report = summarize(lines, now=NOW)
    plan = report["api"]["trip-plan"]
    assert plan["requests"] == 35
    assert plan["availability_samples"] == 5
    assert plan["server_errors"] == 3
    assert plan["client_errors"] == 30
    assert plan["server_error_percent"] == 60
    assert report["check"]["violations"] == [
        {
            "operation": "trip-plan",
            "server_errors": 3,
            "availability_samples": 5,
            "server_error_percent": 60,
        }
    ]


def test_window_boundaries_unknown_lines_and_custom_prefix() -> None:
    """Принимает только нужный период UTC и точные рабочие маршруты."""

    report = summarize(
        [
            http_line(503, at=NOW - timedelta(minutes=30, seconds=1)),
            http_line(503, at=NOW + timedelta(seconds=1)),
            http_line(at=NOW - timedelta(minutes=30)).replace("/api/v1/", "/internal/"),
            http_line().replace("/api/v1/", "/internal/"),
            http_line(),
            http_line(operation="trip-plan-extra"),
            http_line(operation="health/ready").replace("method=POST", "method=GET"),
            log_line(PRIVATE, logger="app.main"),
            PRIVATE,
        ],
        now=NOW,
        api_prefix="/internal",
    )
    assert report["api"]["trip-plan"]["requests"] == 2
    assert report["input"]["invalid_events"] == 0
    assert PRIVATE not in json.dumps(report)


def test_empty_and_small_samples_do_not_claim_availability() -> None:
    """Тишина и четыре ошибки не доказывают исправность или массовый сбой."""

    empty = summarize([], now=NOW)
    assert empty["check"]["status"] == "insufficient_data"
    assert empty["api"]["trip-plan"]["server_error_percent"] is None
    assert empty["llm"]["latency_ms"] == {"samples": 0, "p50": None, "p95": None}
    assert summarize([http_line(503)] * 4, now=NOW)["check"]["violations"] == []
    assert (
        summarize(
            [http_line(503)] * 2 + [http_line()] * 2,
            now=NOW,
            minimum_requests=4,
        )["check"]["status"]
        == "degraded"
    )


def test_multiple_models_usage_and_nearest_rank_latency() -> None:
    """Разделяет модели и не заменяет отсутствующий usage выдуманным расходом."""

    report = summarize(
        [
            call_line(model="openai/gpt-oss-20b", duration_ms=10),
            call_line(duration_ms=500, prompt_tokens=None),
            call_line(duration_ms=1000, attempt=2),
        ],
        now=NOW,
    )
    assert report["llm"]["latency_ms"] == {"samples": 3, "p50": 500, "p95": 1000}
    assert report["llm"]["by_model"]["openai/gpt-oss-20b"]["calls"] == 1
    assert report["llm"]["usage"]["known_prompt_tokens"] == 20
    assert report["llm"]["usage"]["calls_with_missing_usage"] == 1
    assert latency_summary([3, 1, 2, 4]) == {"samples": 4, "p50": 2, "p95": 4}


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_ms": True},
        {"duration_ms": float("nan")},
        {"prompt_tokens": -1},
        {"completion_tokens": True},
        {"attempt": 0},
        {"provider_attempt": "2"},
        {"model": PRIVATE + "\n"},
        {"outcome": PRIVATE},
    ],
)
def test_invalid_known_events_fail_closed_without_exporting_payload(
    changes: dict[str, object],
) -> None:
    """Повреждённая метрика не создаёт ложную здоровую сводку."""

    report = summarize([call_line(**changes)], now=NOW)
    assert report["input"]["invalid_events"] == 1
    assert report["llm"]["calls"] == 0
    assert report["check"]["status"] == "invalid_input"
    assert PRIVATE not in json.dumps(report)


def test_malformed_json_is_not_echoed() -> None:
    """Сводка сообщает число повреждений без исходных строк."""

    report = summarize(
        [log_line("LLM call | " + PRIVATE), log_line('LLM retry | {"event":"wrong"}')],
        now=NOW,
    )
    assert report["input"]["invalid_events"] == 2
    assert PRIVATE not in json.dumps(report)


def test_current_llm_logger_contract_and_privacy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Читает событие настоящего logger, исключая идентификаторы и чужие поля."""

    caplog.set_level(logging.INFO, logger="app.services.ai")
    _log_llm_call(
        level=logging.INFO,
        outcome="success",
        metadata=LLMResponseMetadata(
            model="openai/gpt-oss-120b",
            request_id=PRIVATE,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            finish_reason="stop",
        ),
        attempt=1,
        duration_ms=230,
    )
    lines = [log_line(record.getMessage()) for record in caplog.records]
    lines += [call_line(prompt=PRIVATE, response=PRIVATE, api_key=PRIVATE)]
    report = summarize(lines, now=NOW)
    assert report["llm"]["calls"] == 2
    assert report["llm"]["usage"]["known_total_tokens"] == 180
    assert PRIVATE not in json.dumps(report)
    assert "private-correlation-id" not in json.dumps(report)


@pytest.mark.asyncio
async def test_current_http_middleware_contract(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Читает реальный итоговый 502 FastAPI, не response body и не access log."""

    async def fail_analysis(**_: object) -> TripIntakeResponse:
        raise AIServiceError(PRIVATE)

    monkeypatch.setattr(trip_api, "process_trip_message", fail_analysis)
    caplog.set_level(logging.INFO, logger="app.main")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/trip-intake",
            json={"telegram_id": 123456, "user_message": PRIVATE, "draft": {}},
        )
    assert response.status_code == 502
    report = summarize(
        [log_line(record.getMessage(), logger="app.main") for record in caplog.records],
        now=NOW,
        minimum_requests=1,
    )
    assert report["api"]["trip-intake"]["statuses"] == {"502": 1}
    assert report["check"]["status"] == "degraded"
    assert PRIVATE not in json.dumps(report)


@pytest.mark.parametrize(
    "scenario, expected", [("bad", 1), ("empty", 0), ("invalid", 2)]
)
def test_cli_exit_codes_without_loading_application_settings(
    scenario: str, expected: int
) -> None:
    """CLI работает на стандартной библиотеке даже с неверными Settings."""

    now = datetime.now(UTC)
    source = {
        "bad": "".join([http_line(503, at=now)] * 5),
        "empty": "",
        "invalid": log_line("LLM call | " + PRIVATE, at=now),
    }[scenario]
    result = subprocess.run(
        [sys.executable, "scripts/report_production.py", "--check"],
        cwd=PROJECT_ROOT,
        input=source,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": "invalid", "LLM_API_KEY": ""},
        timeout=10,
    )
    assert result.returncode == expected
    assert json.loads(result.stdout)["schema_version"] == 1
    assert PRIVATE not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["--window-minutes", "0"],
        ["--minimum-requests", "0"],
        ["--max-server-error-percent", "101"],
        ["--api-prefix", "/api/v1/"],
    ],
)
def test_cli_rejects_invalid_thresholds(arguments: list[str]) -> None:
    """Неверные параметры завершаются до чтения логов."""

    result = subprocess.run(
        [sys.executable, "scripts/report_production.py", *arguments],
        cwd=PROJECT_ROOT,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.fixture
def monitor_environment(tmp_path: Path) -> dict[str, str]:
    """Подменяет системные команды и уведомления; запускает настоящий shell-скрипт."""

    if sys.platform != "linux" or shutil.which("bash") is None:
        pytest.skip("Shell-интеграция запускается на Linux в CI.")
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "hankego-test.dump").write_text("test-backup", encoding="utf-8")
    (backups / "hankego-test.dump.sha256").write_text("test-checksum", encoding="utf-8")
    commands = {
        "docker": (
            "import os, sys\nfrom pathlib import Path\n"
            "if 'logs' in sys.argv:\n"
            "    target = sys.stderr if os.environ.get('TEST_LOG_STREAM') == 'stderr' else sys.stdout\n"
            "    print(Path(os.environ['TEST_LOGS']).read_text(), end='', file=target)\n"
            "    raise SystemExit(int(os.environ.get('TEST_LOG_EXIT', '0')))\n"
            "if 'inspect' in sys.argv:\n"
            "    print('healthy' if 'Health' in str(sys.argv) else 'running')\n"
            "else:\n    print('test-container')\n"
        ),
        "curl": "",
        "df": "print('Filesystem blocks used available capacity mount')\nprint('test 100 10 90 10% /')\n",
        "systemctl": "import sys\nif 'show' in sys.argv: print('success')\n",
        "python3": (
            "import json, os, sys\n"
            "if 'MONITORING_MESSAGE' in os.environ:\n"
            "    with open(os.environ['TEST_NOTIFICATIONS'], 'a') as target:\n"
            "        target.write(json.dumps(os.environ['MONITORING_MESSAGE']) + '\\n')\n"
            "else:\n"
            f"    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
        ),
    }
    for name, source in commands.items():
        target = command_dir / name
        target.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        target.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{command_dir}{os.pathsep}{os.environ['PATH']}",
        "HANKEGO_APP_DIR": str(PROJECT_ROOT),
        "HANKEGO_BACKUP_DIR": str(backups),
        "STATE_DIRECTORY": str(state),
        "TELEGRAM_BOT_TOKEN": "fake-test-token",
        "MONITORING_TELEGRAM_CHAT_ID": "fake-test-chat",
        "HANKEGO_AI_WINDOW_MINUTES": "30",
        "HANKEGO_AI_MIN_REQUESTS": "5",
        "HANKEGO_AI_MAX_SERVER_ERROR_PERCENT": "50",
        "API_PREFIX": "/api/v1",
        "TEST_LOGS": str(tmp_path / "api.log"),
        "TEST_NOTIFICATIONS": str(tmp_path / "notifications.jsonl"),
    }


def run_monitor(
    environment: dict[str, str], logs: str
) -> subprocess.CompletedProcess[str]:
    """Фиксирует вход и запускает скрипт; внешние операции перехвачены фикстурой."""

    Path(environment["TEST_LOGS"]).write_text(logs, encoding="utf-8")
    return subprocess.run(
        ["bash", "scripts/monitor_production.sh"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_monitor_alert_deduplication_and_window_recovery(
    monitor_environment: dict[str, str],
) -> None:
    """Порог включает существующее уведомление один раз и сбрасывается при норме."""

    now = datetime.now(UTC)
    bad_logs = "".join([http_line(503, at=now)] * 5)
    assert run_monitor(monitor_environment, bad_logs).returncode == 1
    assert run_monitor(monitor_environment, bad_logs).returncode == 1
    notifications = Path(monitor_environment["TEST_NOTIFICATIONS"])
    messages = [json.loads(line) for line in notifications.read_text().splitlines()]
    assert len(messages) == 1
    assert "trip-plan: 5/5" in messages[0]
    assert (
        run_monitor(monitor_environment, "".join([http_line(at=now)] * 5)).returncode
        == 0
    )
    assert len(notifications.read_text().splitlines()) == 2
    report_path = Path(monitor_environment["STATE_DIRECTORY"]) / "ai-report.json"
    assert json.loads(report_path.read_text())["check"]["status"] == "healthy"
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert list(report_path.parent.glob("ai-report.*")) == [report_path]


@pytest.mark.parametrize("failure", ["collection", "parser"])
def test_monitor_does_not_replace_valid_report_after_collection_or_parser_error(
    monitor_environment: dict[str, str], failure: str
) -> None:
    """Ошибки чтения не маскируются нулевой статистикой и не раскрывают сырой лог."""

    assert run_monitor(monitor_environment, "").returncode == 0
    report_path = Path(monitor_environment["STATE_DIRECTORY"]) / "ai-report.json"
    previous = report_path.read_bytes()
    if failure == "collection":
        monitor_environment["TEST_LOG_EXIT"] = "124"
        logs = ""
    else:
        logs = log_line("LLM call | " + PRIVATE, at=datetime.now(UTC))
    result = run_monitor(monitor_environment, logs)
    assert result.returncode == 1
    assert report_path.read_bytes() == previous
    assert PRIVATE not in result.stdout + result.stderr
    assert PRIVATE not in Path(monitor_environment["TEST_NOTIFICATIONS"]).read_text()


def test_monitor_inactive_window_and_invalid_configuration(
    monitor_environment: dict[str, str],
) -> None:
    """При отсутствии запросов не тревожит; неверный порог не запускает проверки."""

    assert run_monitor(monitor_environment, "").returncode == 0
    assert not Path(monitor_environment["TEST_NOTIFICATIONS"]).exists()
    report_path = Path(monitor_environment["STATE_DIRECTORY"]) / "ai-report.json"
    assert json.loads(report_path.read_text())["check"]["status"] == "insufficient_data"
    monitor_environment["HANKEGO_AI_MIN_REQUESTS"] = "0"
    assert run_monitor(monitor_environment, "").returncode == 2
    assert not Path(monitor_environment["TEST_NOTIFICATIONS"]).exists()


def test_monitor_reads_container_stderr(monitor_environment: dict[str, str]) -> None:
    """StreamHandler пишет в stderr: такие события тоже входят в мониторинг."""

    monitor_environment["TEST_LOG_STREAM"] = "stderr"
    logs = "".join([http_line(502, at=datetime.now(UTC))] * 5)
    result = run_monitor(monitor_environment, logs)
    assert result.returncode == 1
    report_path = Path(monitor_environment["STATE_DIRECTORY"]) / "ai-report.json"
    assert json.loads(report_path.read_text())["api"]["trip-plan"]["server_errors"] == 5
