"""Сводка безопасных событий API и LLM из stdin без загрузки настроек приложения."""

import argparse
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Хостовый монитор работает и с системным Python 3.10, без окружения приложения.
UTC = timezone.utc  # noqa: UP017

OPERATIONS = ("trip-intake", "trip-plan", "trip-edit")
OUTCOMES = {
    "success",
    "timeout",
    "rate_limited",
    "http_error",
    "connection_error",
    "invalid_response",
    "invalid_output",
    "semantic_validation_failed",
}
LOG_HEADER = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) \| "
    r"(?P<logger>app\.main|app\.services\.ai) \| "
    r"correlation_id=[^|\r\n]+ \| (?P<message>[^\r\n]*)"
)
HTTP_EVENT = re.compile(
    r"HTTP request (?P<result>completed|failed) \| method=POST \| "
    r"path=(?P<path>/[^|\s]*) \| "
    r"(?:status=(?P<status>\d{3}) \| )?duration_ms=(?P<duration>\d+(?:\.\d+)?)$"
)
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


def non_negative_number(value: object) -> float:
    """Не принимает bool, отрицательные и неограниченные числа."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("Invalid numeric metric")
    return float(value)


def token_count(value: object) -> int | None:
    """Сохраняет отсутствие usage вместо подмены нулём."""

    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Invalid token metric")
    return value


def latency_summary(values: list[float]) -> dict[str, int | float | None]:
    """Считает nearest-rank перцентили; пустая выборка остаётся неизвестной."""

    ordered = sorted(values)
    return {
        "samples": len(ordered),
        **{
            f"p{percentile}": (
                round(ordered[math.ceil(len(ordered) * percentile / 100) - 1], 2)
                if ordered
                else None
            )
            for percentile in (50, 95)
        },
    }


@dataclass
class Calls:
    """Накапливает метрики попыток одного или всех вариантов модели."""

    outcomes: Counter[str] = field(default_factory=Counter)
    durations: list[float] = field(default_factory=list)
    known_tokens: Counter[str] = field(default_factory=Counter)
    complete_usage: int = 0
    retry_calls: int = 0

    def add(self, event: dict[str, Any]) -> None:
        """Принимает только предварительно проверенное событие."""

        self.outcomes[event["outcome"]] += 1
        self.durations.append(event["duration_ms"])
        self.retry_calls += event["attempt"] > 1 or event["provider_attempt"] > 1
        self.complete_usage += all(
            event[key] is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if event[key] is not None:
                self.known_tokens[key] += event[key]

    def report(self) -> dict[str, Any]:
        """Не включает исходные строки, request ID и тексты исключений."""

        return {
            "calls": len(self.durations),
            "outcomes": dict(sorted(self.outcomes.items())),
            "retry_calls": self.retry_calls,
            "latency_ms": latency_summary(self.durations),
            "usage": {
                "calls_with_complete_usage": self.complete_usage,
                "calls_with_missing_usage": len(self.durations) - self.complete_usage,
                **{
                    f"known_{key}": self.known_tokens[key]
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
            },
        }


@dataclass
class Requests:
    """Считает итоговые HTTP-ответы отдельно от попыток модели."""

    statuses: Counter[int] = field(default_factory=Counter)
    durations: list[float] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        """Исключает клиентские ошибки из знаменателя доступности сервиса."""

        successful = sum(
            n for status, n in self.statuses.items() if 200 <= status < 300
        )
        server_errors = sum(n for status, n in self.statuses.items() if status >= 500)
        eligible = successful + server_errors
        return {
            "requests": sum(self.statuses.values()),
            "successful": successful,
            "server_errors": server_errors,
            "client_errors": sum(
                n for status, n in self.statuses.items() if 400 <= status < 500
            ),
            "availability_samples": eligible,
            "server_error_percent": (
                round(server_errors / eligible * 100, 2) if eligible else None
            ),
            "statuses": {str(key): n for key, n in sorted(self.statuses.items())},
            "latency_ms": latency_summary(self.durations),
        }


def validate_call(payload: object) -> dict[str, Any]:
    """Отбирает фиксированные поля, не копируя произвольный provider payload."""

    if not isinstance(payload, dict) or payload.get("event") != "llm_call":
        raise ValueError("Invalid LLM event")
    model = payload.get("model")
    outcome = payload.get("outcome")
    if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
        raise ValueError("Invalid model identifier")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValueError("Unknown LLM outcome")
    result = {
        "model": model,
        "outcome": outcome,
        "duration_ms": non_negative_number(payload.get("duration_ms")),
        **{
            key: token_count(payload.get(key))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
    }
    for key in ("attempt", "provider_attempt"):
        value = payload.get(key)
        if type(value) is not int or value < 1:
            raise ValueError("Invalid attempt number")
        result[key] = value
    return result


def summarize(
    lines: Iterable[str],
    *,
    now: datetime,
    window_minutes: int = 30,
    minimum_requests: int = 5,
    max_server_error_percent: int = 50,
    api_prefix: str = "/api/v1",
) -> dict[str, Any]:
    """Читает UTC-логи приложения, в том числе с префиксом Docker Compose."""

    since = now - timedelta(minutes=window_minutes)
    calls = Calls()
    models: dict[str, Calls] = {}
    requests = {operation: Requests() for operation in OPERATIONS}
    retry_reasons: Counter[str] = Counter()
    invalid_events = 0
    line_count = 0
    for line in lines:
        line_count += 1
        header = LOG_HEADER.search(line)
        if header is None:
            continue
        message = header["message"]
        logger = header["logger"]
        is_llm = logger == "app.services.ai" and message.startswith("LLM call | ")
        is_retry = logger == "app.services.ai" and message.startswith("LLM retry | ")
        http = HTTP_EVENT.fullmatch(message) if logger == "app.main" else None
        operation = (
            http["path"].removeprefix(f"{api_prefix}/") if http is not None else None
        )
        is_http = operation in requests and http["path"] == f"{api_prefix}/{operation}"
        if not (is_llm or is_retry or is_http):
            continue
        try:
            timestamp = datetime.strptime(
                header["timestamp"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
            if not since <= timestamp <= now:
                continue
            if is_llm:
                event = validate_call(json.loads(message.removeprefix("LLM call | ")))
                calls.add(event)
                models.setdefault(event["model"], Calls()).add(event)
            elif is_retry:
                event = json.loads(message.removeprefix("LLM retry | "))
                if (
                    not isinstance(event, dict)
                    or event.get("event") != "llm_retry"
                    or event.get("reason")
                    not in ("rate_limit", "structured_output_validation")
                ):
                    raise ValueError("Invalid retry event")
                retry_reasons[event["reason"]] += 1
            else:
                status = int(http["status"]) if http["status"] else 500
                if not 100 <= status <= 599 or (
                    http["result"] == "completed" and http["status"] is None
                ):
                    raise ValueError("Invalid HTTP status")
                duration = non_negative_number(float(http["duration"]))
                requests[operation].statuses[status] += 1
                requests[operation].durations.append(duration)
        except (ValueError, TypeError, OverflowError):
            invalid_events += 1

    api = {operation: stats.report() for operation, stats in requests.items()}
    violations = []
    for operation, stats in api.items():
        eligible = stats["availability_samples"]
        # Сравниваем точную долю, а не округлённое значение для отображения.
        if (
            eligible >= minimum_requests
            and stats["server_errors"] * 100 >= max_server_error_percent * eligible
        ):
            violations.append(
                {
                    "operation": operation,
                    "server_errors": stats["server_errors"],
                    "availability_samples": eligible,
                    "server_error_percent": stats["server_error_percent"],
                }
            )
    return {
        "schema_version": 1,
        "window": {"from_utc": since.isoformat(), "to_utc": now.isoformat()},
        "input": {"lines": line_count, "invalid_events": invalid_events},
        "api": api,
        "llm": {
            **calls.report(),
            "by_model": {k: v.report() for k, v in sorted(models.items())},
        },
        "scheduled_retries": dict(sorted(retry_reasons.items())),
        "check": {
            "status": (
                "invalid_input"
                if invalid_events
                else "degraded"
                if violations
                else "healthy"
                if any(
                    s["availability_samples"] >= minimum_requests for s in api.values()
                )
                else "insufficient_data"
            ),
            "minimum_requests_per_operation": minimum_requests,
            "max_server_error_percent": max_server_error_percent,
            "violations": violations,
        },
    }


def bounded_integer(minimum: int, maximum: int) -> Callable[[str], int]:
    """Создаёт argparse-валидатор без зависимостей от Settings и .env."""

    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"Expected {minimum}..{maximum}")
        return number

    return parse


def main(argv: list[str] | None = None) -> int:
    """Выводит JSON; проверка порога возвращает 1, ошибки входа — 2."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-minutes", type=bounded_integer(1, 1440), default=30)
    parser.add_argument(
        "--minimum-requests", type=bounded_integer(1, 100000), default=5
    )
    parser.add_argument(
        "--max-server-error-percent", type=bounded_integer(1, 100), default=50
    )
    parser.add_argument("--api-prefix", default="/api/v1")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"/[A-Za-z0-9_/-]*[A-Za-z0-9_-]", args.api_prefix):
        parser.error("Expected an API prefix without a trailing slash")
    try:
        report = summarize(
            sys.stdin,
            now=datetime.now(UTC),
            window_minutes=args.window_minutes,
            minimum_requests=args.minimum_requests,
            max_server_error_percent=args.max_server_error_percent,
            api_prefix=args.api_prefix,
        )
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    except (OSError, UnicodeError):
        print("ERROR: could not read monitoring input", file=sys.stderr)
        return 2
    if report["input"]["invalid_events"]:
        return 2
    return int(args.check and bool(report["check"]["violations"]))


if __name__ == "__main__":
    raise SystemExit(main())
