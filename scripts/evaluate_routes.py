"""Оценка настоящей LLM на фиксированных сценариях маршрутов и intake.

Запуск без --live только проверяет и перечисляет сценарии. Корпус routes
начинается с готовых TripPreferences и оценивает итоговый публичный план.
Корпус intake проверяет сервис разбора сообщений и переходы черновика.
HTTP, Redis, БД и получение геоданных не входят в оба вида оценки.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import platform
from collections import Counter
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.config import get_settings
from app.schemas.geoapify import PlaceCandidate, TravelContext
from app.schemas.trip import (
    RequiredTripField,
    StrictSchema,
    TripDraft,
    TripIntakeResponse,
    TripIntent,
    TripPlanResponse,
    TripPreferences,
)
from app.services import ai, trip_intake
from app.services.place_geography import calculate_distance_meters
from app.services.trip_enrichment import place_matches_category

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "route_cases.json"
INTAKE_CASES_PATH = ROOT / "evals" / "intake_cases.json"
PERIODS = ("morning", "afternoon", "evening")


class Expectations(StrictSchema):
    """Критерии корпуса не зависят от предпочтений, возвращённых моделью."""

    required_place_ids: list[str] = Field(default_factory=list)
    interest_categories: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    minimum_places_per_day: int = Field(default=2, ge=1)
    minimum_unique_places: int = Field(default=2, ge=1)
    maximum_daily_distance_meters: float = Field(default=4_000.0, gt=0)
    general_evening_only: bool = False


class EvaluationCase(StrictSchema):
    """Один постоянный сценарий генерации или разбора изменения."""

    id: str = Field(min_length=1)
    kind: Literal["generate", "edit", "reject_edit"]
    user_message: str = Field(min_length=1)
    preferences: TripPreferences
    context: str
    current_plan: str | None = None
    expectations: Expectations

    @model_validator(mode="after")
    def validate_edit(self) -> Self:
        """Редактирование требует исходного плана."""

        if (self.kind != "generate") != (self.current_plan is not None):
            raise ValueError("Только edit и reject_edit требуют current_plan.")
        return self


class EvaluationSuite(StrictSchema):
    """Версионированный корпус с проверяемыми ссылками на контексты и места."""

    version: Literal[1]
    notes: str
    contexts: dict[str, TravelContext]
    plans: dict[str, TripPlanResponse]
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        """Не позволяет незаметно оценивать ошибочно настроенный сценарий."""

        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Идентификаторы сценариев должны быть уникальными.")
        for context in self.contexts.values():
            for values in (
                [place.source_place_id for place in context.places],
                [place.display_name for place in context.places],
            ):
                if len(set(values)) != len(values):
                    raise ValueError(
                        "ID и отображаемые имена мест должны быть уникальны."
                    )
        for case in self.cases:
            context = self.contexts.get(case.context)
            if context is None:
                raise ValueError("Неизвестный контекст сценария.")
            available = {place.source_place_id for place in context.places}
            if not set(case.expectations.required_place_ids).issubset(available):
                raise ValueError("Обязательное место отсутствует в контексте.")
            if case.current_plan is not None:
                plan = self.plans.get(case.current_plan)
                if plan is None or (
                    plan.destination != case.preferences.destination
                    or plan.duration_days != case.preferences.duration_days
                ):
                    raise ValueError("Исходный план не соответствует сценарию.")
        return self


def load_suite() -> EvaluationSuite:
    """Читает корпус без создания Settings и без сетевых запросов."""

    return EvaluationSuite.model_validate_json(CASES_PATH.read_bytes())


class IntakeExpectations(StrictSchema):
    """Ожидаемый контракт сервиса; варианты формулировок интересов допустимы."""

    intent: TripIntent
    draft: TripDraft
    missing_required_fields: list[RequiredTripField]
    ready_to_generate: bool
    next_question_for: RequiredTripField | None
    interests_contains: list[str] = Field(default_factory=list)
    interests_excludes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_criteria(self) -> Self:
        """Не допускает критериев, которые скрывают пустые или неверные ожидания."""

        terms = [*self.interests_contains, *self.interests_excludes]
        if any(not term.strip() for term in terms):
            raise ValueError("Подстроки интересов не должны быть пустыми.")
        if self.interests_contains and self.draft.interests is None:
            raise ValueError("Подстроки требуют ожидаемого значения interests.")
        missing = [
            name
            for name in ("destination", "duration_days")
            if getattr(self.draft, name) is None
        ]
        if self.missing_required_fields != missing:
            raise ValueError("Ожидаемые пропуски не соответствуют черновику.")
        ready = self.intent == "plan_trip" and not missing
        question = missing[0] if self.intent == "plan_trip" and missing else None
        if self.ready_to_generate != ready or self.next_question_for != question:
            raise ValueError("Готовность или вопрос не соответствуют ожиданиям.")
        if self.intent == "cancel" and self.draft != TripDraft():
            raise ValueError("Отмена должна ожидать пустой черновик.")
        return self


class IntakeStep(StrictSchema):
    """Одна реплика и вручную заданные ожидания после её обработки."""

    user_message: str = Field(min_length=1, max_length=2000)
    expectations: IntakeExpectations


class IntakeEvaluationCase(StrictSchema):
    """Следующая реплика использует настоящий ответ предыдущей, не эталон."""

    id: str = Field(min_length=1)
    kind: Literal["intake"] = "intake"
    initial_draft: TripDraft = Field(default_factory=TripDraft)
    steps: list[IntakeStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dialogue(self) -> Self:
        """Корпус не продолжает диалог после готовности или смены действия."""

        for step in self.steps[:-1]:
            expected = step.expectations
            if expected.ready_to_generate or expected.intent != "plan_trip":
                raise ValueError("После завершения диалога не должно быть реплик.")
        return self


class IntakeEvaluationSuite(StrictSchema):
    """Версионированный корпус сообщений без личных данных пользователя."""

    version: Literal[1]
    notes: str
    cases: list[IntakeEvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        """Не позволяет смешать результаты одноимённых сценариев."""

        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Идентификаторы сценариев должны быть уникальными.")
        return self


def load_intake_suite() -> IntakeEvaluationSuite:
    """Проверяет сообщения и ожидания до чтения настроек и обращения к LLM."""

    return IntakeEvaluationSuite.model_validate_json(INTAKE_CASES_PATH.read_bytes())


def normalize_text(value: str | None) -> str | None:
    """Игнорирует только регистр и различия пробелов, сохраняя смысл проверки."""

    return " ".join(value.casefold().split()) if value is not None else None


def assess_intake(
    response: TripIntakeResponse, expected: IntakeExpectations
) -> dict[str, bool]:
    """Сравнивает публичный результат с независимыми ожиданиями корпуса."""

    checks = {
        "intent": response.intent == expected.intent,
        "missing_required_fields": response.missing_required_fields
        == expected.missing_required_fields,
        "ready_to_generate": response.ready_to_generate == expected.ready_to_generate,
        "next_question": response.next_question
        == (
            trip_intake.NEXT_QUESTIONS[expected.next_question_for]
            if expected.next_question_for is not None
            else None
        ),
    }
    for field in ("destination", "duration_days", "travel_period", "budget"):
        actual = getattr(response.draft, field)
        wanted = getattr(expected.draft, field)
        if isinstance(actual, str):
            actual = normalize_text(actual)
        if isinstance(wanted, str):
            wanted = normalize_text(wanted)
        checks[f"draft:{field}"] = actual == wanted
    checks["draft:must_visit_places"] = Counter(
        normalize_text(name) for name in response.draft.must_visit_places
    ) == Counter(normalize_text(name) for name in expected.draft.must_visit_places)
    interests = normalize_text(response.draft.interests)
    checks["draft:interests"] = (
        interests is not None
        and all(
            normalize_text(term) in interests for term in expected.interests_contains
        )
        if expected.interests_contains
        else interests == normalize_text(expected.draft.interests)
    )
    checks["draft:interests_excludes"] = all(
        normalize_text(term) not in (interests or "")
        for term in expected.interests_excludes
    )
    return checks


async def evaluate_intake_dialogue(
    case: IntakeEvaluationCase, result: dict[str, Any], *, interval: float
) -> dict[str, Any]:
    """Проверяет реальный сервис и прекращает сценарий на первой ошибке."""

    draft = case.initial_draft.model_copy(deep=True)
    result["steps"] = []
    result["checks"] = {}
    for index, step in enumerate(case.steps, start=1):
        if index > 1:
            print(f"Пауза перед следующей репликой: {interval:g} с.", flush=True)
            await asyncio.sleep(interval)
        turn: dict[str, Any] = {
            "step": index,
            "user_message": step.user_message,
            "input_draft": draft.model_dump(mode="json"),
            "expectations": step.expectations.model_dump(mode="json"),
        }
        result["steps"].append(turn)
        response = await trip_intake.process_trip_message(
            user_message=step.user_message, draft=draft
        )
        checks = assess_intake(response, step.expectations)
        turn["response"] = response.model_dump(mode="json")
        turn["checks"] = checks
        result["checks"].update(
            {f"step:{index}:{name}": passed for name, passed in checks.items()}
        )
        if not all(checks.values()):
            result["failed_step"] = index
            return finish_checks(result)
        draft = response.draft.model_copy(deep=True)
    return finish_checks(result)


def selected_places(
    activities: list[str], context: TravelContext
) -> list[PlaceCandidate]:
    """Распознаёт только префиксы конкретных мест публичного форматтера.

    Упоминание в summary не считается посещением. Уникальность имён корпуса
    проверяется заранее; свободный текст общих активностей остаётся для человека.
    """

    return [
        place
        for activity in activities
        for place in context.places
        if activity.startswith(f"{place.display_name}:")
    ]


def assess_plan(
    plan: TripPlanResponse, case: EvaluationCase, context: TravelContext
) -> dict[str, Any]:
    """Оценивает итоговый план независимо от успешного ответа AI-сервиса."""

    daily_places = [
        selected_places(
            [activity for period in PERIODS for activity in getattr(day, period)],
            context,
        )
        for day in plan.days
    ]
    selected = {place.source_place_id: place for day in daily_places for place in day}
    distances = [
        max(
            (
                calculate_distance_meters(
                    first_latitude=first.latitude,
                    first_longitude=first.longitude,
                    second_latitude=second.latitude,
                    second_longitude=second.longitude,
                )
                for first, second in combinations(places, 2)
            ),
            default=0.0,
        )
        for places in daily_places
    ]
    expected = case.expectations
    checks = {
        "destination": plan.destination.casefold()
        == case.preferences.destination.casefold(),
        "duration": plan.duration_days == case.preferences.duration_days,
        "required_places": set(expected.required_place_ids).issubset(selected),
        "places_per_day": all(
            len({place.source_place_id for place in places})
            >= expected.minimum_places_per_day
            for places in daily_places
        ),
        "unique_places": len(selected) >= expected.minimum_unique_places,
        "compact_days": all(
            distance <= expected.maximum_daily_distance_meters for distance in distances
        ),
        "forbidden_categories_absent": not any(
            place_matches_category(place, category)
            for place in selected.values()
            for category in expected.forbidden_categories
        ),
    }
    for category in expected.interest_categories:
        checks[f"interest:{category}"] = any(
            place_matches_category(place, category) for place in selected.values()
        )
    if expected.general_evening_only:
        checks["general_evening_only"] = all(
            not selected_places(day.evening, context) for day in plan.days
        )
    return {
        "checks": checks,
        "selected_place_ids": list(selected),
        "daily_max_distance_meters": [round(value, 1) for value in distances],
    }


class CallCollector(logging.Handler):
    """Собирает только безопасные поля существующих событий llm_call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.msg != "LLM call | %s"
            or not isinstance(record.args, tuple)
            or len(record.args) != 1
        ):
            return
        try:
            event = json.loads(record.args[0])
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(event, dict):
            return
        allowed = (
            "outcome",
            "model",
            "attempt",
            "provider_attempt",
            "duration_ms",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "finish_reason",
            "http_status",
            "validation_reason",
            "provider_error_code",
        )
        self.calls.append({key: event[key] for key in allowed if key in event})


async def evaluate_case(
    case: EvaluationCase | IntakeEvaluationCase,
    suite: EvaluationSuite | IntakeEvaluationSuite,
    *,
    turn_interval: float = 0.0,
) -> dict[str, Any]:
    """Вызывает настоящие AI-функции последовательно, без БД и геопровайдеров."""

    result: dict[str, Any] = {
        "case_id": case.id,
        "kind": case.kind,
    }
    if isinstance(case, IntakeEvaluationCase):
        result["initial_draft"] = case.initial_draft.model_dump(mode="json")
    else:
        result["input_preferences"] = case.preferences.model_dump(mode="json")
        result["expectations"] = case.expectations.model_dump(mode="json")
    collector = CallCollector()
    previous_level = ai.logger.level
    ai.logger.setLevel(logging.INFO)
    ai.logger.addHandler(collector)
    started = perf_counter()
    try:
        if isinstance(case, IntakeEvaluationCase):
            return await evaluate_intake_dialogue(case, result, interval=turn_interval)
        preferences = case.preferences.model_copy(deep=True)
        context = suite.contexts[case.context].model_copy(deep=True)
        if case.kind == "generate":
            plan = await ai.generate_trip_plan(
                preferences=preferences, travel_context=context
            )
        else:
            current_plan = suite.plans[case.current_plan].model_copy(deep=True)
            analysis = await ai.analyze_trip_edit(
                current_preferences=preferences,
                current_plan=current_plan,
                instruction=case.user_message,
            )
            result["analysis"] = analysis.model_dump(mode="json")
            if case.kind == "reject_edit":
                result["checks"] = {"edit_rejected": not analysis.supported}
                return finish_checks(result)
            if not analysis.supported:
                result["checks"] = {"edit_supported": False}
                return finish_checks(result)
            updates: dict[str, object] = {}
            if analysis.interests_changed:
                updates["interests"] = analysis.interests
            if analysis.must_visit_places_changed:
                updates["must_visit_places"] = analysis.must_visit_places
            preferences = preferences.model_copy(update=updates)
            result["updated_preferences"] = preferences.model_dump(mode="json")
            plan = await ai.generate_edited_trip_plan(
                preferences=preferences,
                travel_context=context,
                current_plan=current_plan,
                instruction=case.user_message,
            )
        result.update(assess_plan(plan, case, context))
        result["plan"] = plan.model_dump(mode="json")
        return finish_checks(result)
    except ai.AIServiceError as error:
        result["status"] = "service_error"
        result["error_type"] = type(error).__name__
        return result
    except Exception as error:
        # Ошибка инструмента не считается ошибкой модели; текст может быть секретным.
        result["status"] = "runner_error"
        result["error_type"] = type(error).__name__
        return result
    finally:
        ai.logger.removeHandler(collector)
        ai.logger.setLevel(previous_level)
        result["duration_ms"] = round((perf_counter() - started) * 1000)
        result["calls"] = collector.calls
        result["retry_used"] = any(
            call.get("attempt", 1) > 1 or call.get("provider_attempt", 1) > 1
            for call in collector.calls
        )
        result["known_total_tokens"] = sum(
            call["total_tokens"]
            for call in collector.calls
            if call.get("total_tokens") is not None
        )
        result["usage_complete"] = bool(collector.calls) and all(
            call.get("total_tokens") is not None for call in collector.calls
        )


def finish_checks(result: dict[str, Any]) -> dict[str, Any]:
    """Успешный API-ответ не заменяет отдельную проверку критериев корпуса."""

    result["status"] = "passed" if all(result["checks"].values()) else "quality_failed"
    return result


def save_report(path: Path, report: dict[str, Any]) -> None:
    """Сохраняет только завершённые прогоны после каждого сценария."""

    report["summary"] = dict(Counter(row["status"] for row in report["results"]))
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def run_suite(
    cases: list[EvaluationCase] | list[IntakeEvaluationCase],
    suite: EvaluationSuite | IntakeEvaluationSuite,
    *,
    repeats: int,
    interval: float,
    path: Path,
    report: dict[str, Any],
) -> None:
    """Оставляет частичный отчёт при остановке и не добавляет внешних retry."""

    save_report(path, report)
    for repeat in range(1, repeats + 1):
        for case in cases:
            if report["results"]:
                print(f"Пауза перед следующим сценарием: {interval:g} с.", flush=True)
                await asyncio.sleep(interval)
            if isinstance(case, IntakeEvaluationCase):
                row = await evaluate_case(case, suite, turn_interval=interval)
            else:
                row = await evaluate_case(case, suite)
            row["repeat"] = repeat
            report["results"].append(row)
            save_report(path, report)
            print(
                f"{case.id} [{repeat}/{repeats}]: {row['status']} | "
                f"calls={len(row['calls'])} | retry={row['retry_used']}",
                flush=True,
            )
            failed = [
                name for name, passed in row.get("checks", {}).items() if not passed
            ]
            if failed:
                print(f"Не прошли: {', '.join(failed)}", flush=True)
    report["completed"] = True
    save_report(path, report)


def main(argv: list[str] | None = None) -> int:
    """Проверяет параметры до первого внешнего вызова и возвращает код результата."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("routes", "intake"), default="routes")
    parser.add_argument("--live", action="store_true", help="Вызвать настоящую LLM.")
    parser.add_argument(
        "--case", action="append", default=[], help="ID сценария или all."
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=65.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat должен быть от 1 до 10.")
    if (
        not math.isfinite(args.interval_seconds)
        or not 0 <= args.interval_seconds <= 600
    ):
        parser.error("--interval-seconds должен быть от 0 до 600.")
    cases_path = INTAKE_CASES_PATH if args.suite == "intake" else CASES_PATH
    try:
        suite = load_intake_suite() if args.suite == "intake" else load_suite()
    except (OSError, ValidationError):
        parser.error(f"Не удалось проверить {cases_path.name}.")
    known_ids = {case.id for case in suite.cases}
    if set(args.case) - known_ids - {"all"}:
        parser.error("Неизвестный --case; запустите без --live для списка.")
    if args.live and not args.case:
        parser.error("Для --live явно укажите --case ID или --case all.")
    cases = [
        case
        for case in suite.cases
        if not args.case or "all" in args.case or case.id in args.case
    ]
    if not args.live:
        for case in cases:
            if isinstance(case, IntakeEvaluationCase):
                print(f"{case.id}: intake | реплик: {len(case.steps)}")
                for index, step in enumerate(case.steps, start=1):
                    print(f"  {index}. {step.user_message}")
            else:
                print(f"{case.id}: {case.kind} | {case.user_message}")
        print(
            "Корпус проверен. Сетевых запросов не было; для оценки модели нужен --live."
        )
        return 0
    path = args.output or ROOT / "evals" / "results" / (
        f"{args.suite}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )
    if path.exists():
        parser.error("Отчёт уже существует; выберите другой --output.")
    try:
        settings = get_settings()
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        for source in [*sorted((ROOT / "app").rglob("*.py")), Path(__file__)]:
            digest.update(source.relative_to(ROOT).as_posix().encode())
            digest.update(source.read_text(encoding="utf-8").encode())
        report = {
            "schema_version": 1,
            "suite": args.suite,
            "created_at": datetime.now(UTC).isoformat(),
            "fixture_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "app_and_runner_sha256": digest.hexdigest(),
            "python_version": platform.python_version(),
            "generation_model": settings.llm_model,
            "analysis_model": settings.llm_analysis_model,
            "planned_runs": len(cases) * args.repeat,
            "interval_seconds": args.interval_seconds,
            "completed": False,
            "results": [],
        }
        if isinstance(suite, IntakeEvaluationSuite):
            report["planned_messages"] = (
                sum(len(case.steps) for case in cases) * args.repeat
            )
        print(f"Отчёт: {path}", flush=True)
        asyncio.run(
            run_suite(
                cases,
                suite,
                repeats=args.repeat,
                interval=args.interval_seconds,
                path=path,
                report=report,
            )
        )
    except KeyboardInterrupt:
        print("Остановлено. Завершённые прогоны сохранены в частичном отчёте.")
        return 130
    except (OSError, ValidationError) as error:
        print(f"Ошибка настройки или записи отчёта: {type(error).__name__}.")
        return 2
    print(f"Результаты: {report['summary']}")
    if report["summary"].get("runner_error"):
        return 2
    return 0 if all(row["status"] == "passed" for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
