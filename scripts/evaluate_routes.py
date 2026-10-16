"""Оценка настоящей LLM на фиксированных сценариях маршрутов и intake.

Запуск без --live только проверяет и перечисляет сценарии. Корпус routes
начинается с готовых TripPreferences и оценивает итоговый публичный план.
Корпус intake проверяет сервис разбора сообщений и переходы черновика.
Корпус edits проверяет последовательные изменения с фактическим состоянием.
HTTP API, Redis, БД и получение геоданных не входят в оценку.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import platform
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.config import get_settings
from app.schemas.geoapify import PlaceCandidate, TravelContext
from app.schemas.grounded_trip import (
    ACTIVITY_FOCUS_DESCRIPTIONS,
    REST_PERIOD_DESCRIPTION,
    GroundedActivityFocus,
)
from app.schemas.trip import (
    RequiredTripField,
    RestPeriod,
    RestPeriodChange,
    StrictSchema,
    TripDayPeriod,
    TripDraft,
    TripEditAnalysis,
    TripIntakeResponse,
    TripIntent,
    TripPeriodScope,
    TripPlanResponse,
    TripPreferences,
)
from app.services import ai, trip_intake
from app.services.place_geography import calculate_distance_meters
from app.services.trip_enrichment import place_matches_category

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "route_cases.json"
INTAKE_CASES_PATH = ROOT / "evals" / "intake_cases.json"
EDIT_CASES_PATH = ROOT / "evals" / "edit_cases.json"
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


class EditPeriodVisits(StrictSchema):
    """Ожидаемые посещения периода и необязательная проверка типа действия."""

    day: int = Field(ge=1, le=30)
    period: TripDayPeriod
    place_ids: list[str] = Field(min_length=1, max_length=2)
    activity_focus: GroundedActivityFocus | None = None


class EditPeriodTerms(StrictSchema):
    """Ограниченная текстовая проверка общей активности, не оценка её качества."""

    day: int = Field(ge=1, le=30)
    period: TripDayPeriod
    contains: list[str] = Field(min_length=1)
    excludes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_terms(self) -> Self:
        if any(not term.strip() for term in [*self.contains, *self.excludes]):
            raise ValueError("Подстроки активности не должны быть пустыми.")
        return self


class EditExpectations(StrictSchema):
    """Ожидания задаются корпусом, а не ответом анализатора или генератора."""

    rest_period_changes: list[RestPeriodChange]
    rest_periods: list[RestPeriod]
    general_activities: list[EditPeriodTerms] = Field(default_factory=list)
    regeneration_periods: list[TripPeriodScope] | None = None
    replacement_visits: list[EditPeriodVisits] = Field(default_factory=list)


class EditStep(StrictSchema):
    """Одна правка и независимые критерии её результата."""

    user_message: str = Field(min_length=1, max_length=2000)
    expectations: EditExpectations


def expanded_period_scopes(
    rules: Sequence[TripPeriodScope], duration: int
) -> set[tuple[int, str]]:
    """Сравнивает смысл правил независимо от общей или подневной записи."""

    return {
        (day, rule.period)
        for rule in rules
        for day in (range(1, duration + 1) if rule.day is None else [rule.day])
    }


def expanded_rest_changes(
    changes: list[RestPeriodChange], duration: int
) -> dict[tuple[int, str], bool]:
    """Общие изменения применяются до исключений для конкретного дня."""

    result: dict[tuple[int, str], bool] = {}
    for change in sorted(changes, key=lambda item: item.day is not None):
        for day in range(1, duration + 1) if change.day is None else [change.day]:
            result[day, change.period] = change.rest
    return result


class EditEvaluationCase(StrictSchema):
    """Последовательность правок без подстановки эталонного промежуточного плана."""

    id: str = Field(min_length=1)
    kind: Literal["edit_dialogue"] = "edit_dialogue"
    preferences: TripPreferences
    context: str
    current_plan: str
    preserved_visits: list[EditPeriodVisits] = Field(min_length=1)
    steps: list[EditStep] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_criteria(self) -> Self:
        duration = self.preferences.duration_days
        scopes = [(visit.day, visit.period) for visit in self.preserved_visits]
        if len(scopes) != len(set(scopes)):
            raise ValueError("Проверки сохранения периодов не должны повторяться.")
        if any(day > duration for day, _ in scopes):
            raise ValueError("Период сохранения вне поездки.")
        state = expanded_period_scopes(self.preferences.rest_periods, duration)
        for step in self.steps:
            expected = step.expectations
            if expected.regeneration_periods is not None:
                keys = [
                    (rule.day, rule.period) for rule in expected.regeneration_periods
                ]
                if len(keys) != len(set(keys)) or any(
                    day is not None and day > duration for day, _ in keys
                ):
                    raise ValueError("Некорректная ожидаемая область генерации.")
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
                must_visit_places=self.preferences.must_visit_places,
                rest_period_changes=expected.rest_period_changes,
            )
            if any(
                rule.day is not None and rule.day > duration
                for rule in expected.rest_period_changes
            ):
                raise ValueError("Изменение отдыха вне поездки.")
            TripPreferences.model_validate(
                {**self.preferences.model_dump(), "rest_periods": expected.rest_periods}
            )
            for cell, enabled in expanded_rest_changes(
                expected.rest_period_changes, duration
            ).items():
                if enabled:
                    state.add(cell)
                else:
                    state.discard(cell)
            if state != expanded_period_scopes(expected.rest_periods, duration):
                raise ValueError("Ожидаемые изменения и правила отдыха противоречат.")
            if state.intersection(scopes):
                raise ValueError("Отдых конфликтует с сохранением посещения.")
            text_scopes = [
                (term.day, term.period) for term in expected.general_activities
            ]
            if len(text_scopes) != len(set(text_scopes)) or any(
                day > duration or (day, period) in state or (day, period) in scopes
                for day, period in text_scopes
            ):
                raise ValueError("Некорректная проверка общей активности.")
            replacement_scopes = [
                (visit.day, visit.period) for visit in expected.replacement_visits
            ]
            if len(replacement_scopes) != len(set(replacement_scopes)) or any(
                day > duration
                or (day, period) in state
                or (day, period) in scopes
                or (day, period) in text_scopes
                for day, period in replacement_scopes
            ):
                raise ValueError("Некорректная проверка заменяемых посещений.")
        return self


class EditEvaluationSuite(StrictSchema):
    """Отдельный корпус редактирования с собственными синтетическими данными."""

    version: Literal[1]
    notes: str
    contexts: dict[str, TravelContext]
    plans: dict[str, TripPlanResponse]
    cases: list[EditEvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Идентификаторы сценариев должны быть уникальными.")
        for context in self.contexts.values():
            for values in (
                [place.source_place_id for place in context.places],
                [place.display_name for place in context.places],
            ):
                if len(values) != len(set(values)):
                    raise ValueError("ID и отображаемые имена должны быть уникальны.")
        for case in self.cases:
            context = self.contexts.get(case.context)
            plan = self.plans.get(case.current_plan)
            if context is None or plan is None:
                raise ValueError("Неизвестный контекст или исходный план.")
            if (plan.destination, plan.duration_days) != (
                case.preferences.destination,
                case.preferences.duration_days,
            ):
                raise ValueError("Исходный план не соответствует сценарию.")
            available = {place.source_place_id for place in context.places}
            if any(
                len(visit.place_ids) != len(set(visit.place_ids))
                or not set(visit.place_ids).issubset(available)
                for visit in [
                    *case.preserved_visits,
                    *(
                        visit
                        for step in case.steps
                        for visit in step.expectations.replacement_visits
                    ),
                ]
            ):
                raise ValueError("Неверные ID сохраняемых посещений.")
        return self


def load_edit_suite() -> EditEvaluationSuite:
    """Проверяет корпус до чтения настроек и первого вызова модели."""

    return EditEvaluationSuite.model_validate_json(EDIT_CASES_PATH.read_bytes())


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
    plan: TripPlanResponse,
    case: EvaluationCase | EditEvaluationCase,
    context: TravelContext,
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
    expected = (
        Expectations(
            required_place_ids=list(
                dict.fromkeys(
                    place_id
                    for visit in case.preserved_visits
                    for place_id in visit.place_ids
                )
            )
        )
        if isinstance(case, EditEvaluationCase)
        else case.expectations
    )
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


def merge_edit_preferences(
    preferences: TripPreferences, analysis: TripEditAnalysis
) -> TripPreferences:
    """Использует то же валидируемое объединение, что сервис редактирования."""

    updates: dict[str, object] = {}
    if analysis.interests_changed:
        updates["interests"] = analysis.interests
    if analysis.must_visit_places_changed:
        updates["must_visit_places"] = analysis.must_visit_places
    updates["rest_periods"] = preferences.rest_periods_after(
        analysis.rest_period_changes
    )
    return TripPreferences.model_validate({**preferences.model_dump(), **updates})


def assess_edit_periods(
    plan: TripPlanResponse,
    case: EditEvaluationCase,
    expected: EditExpectations,
    context: TravelContext,
) -> dict[str, bool]:
    """Проверяет конкретные периоды; упоминание музея в сводке не считается."""

    by_day = {day.day: day for day in plan.days}

    def activities(day: int, period: str) -> list[str]:
        return getattr(by_day[day], period) if day in by_day else []

    checks = {}
    for visit in case.preserved_visits:
        actual = selected_places(activities(visit.day, visit.period), context)
        checks[f"preserved:{visit.day}:{visit.period}"] = [
            place.source_place_id for place in actual
        ] == visit.place_ids
    for kind, visits in (
        ("preserved", case.preserved_visits),
        ("replacement", expected.replacement_visits),
    ):
        for visit in visits:
            lines = activities(visit.day, visit.period)
            places = selected_places(lines, context)
            if kind == "replacement":
                checks[f"replacement:{visit.day}:{visit.period}"] = [
                    place.source_place_id for place in places
                ] == visit.place_ids
            if visit.activity_focus is not None:
                checks[f"{kind}_focus:{visit.day}:{visit.period}"] = len(lines) == len(
                    places
                ) == len(visit.place_ids) and all(
                    line.startswith(
                        f"{place.display_name}: "
                        f"{ACTIVITY_FOCUS_DESCRIPTIONS[visit.activity_focus]}."
                    )
                    for line, place in zip(lines, places, strict=True)
                )
    reserved = expanded_period_scopes(
        expected.rest_periods, case.preferences.duration_days
    )
    for day, period in sorted(reserved):
        checks[f"quiet_rest:{day}:{period}"] = activities(day, period) == [
            REST_PERIOD_DESCRIPTION
        ]
    checks["unexpected_quiet_rest_absent"] = all(
        REST_PERIOD_DESCRIPTION not in activities(day, period)
        for day in range(1, case.preferences.duration_days + 1)
        for period in PERIODS
        if (day, period) not in reserved
    )
    for terms in expected.general_activities:
        period_activities = activities(terms.day, terms.period)
        text = normalize_text(" ".join(period_activities)) or ""
        checks[f"general_activity:{terms.day}:{terms.period}"] = (
            bool(period_activities)
            and not selected_places(period_activities, context)
            and all(normalize_text(term) in text for term in terms.contains)
            and all(normalize_text(term) not in text for term in terms.excludes)
        )
    return checks


async def evaluate_edit_dialogue(
    case: EditEvaluationCase,
    suite: EditEvaluationSuite,
    result: dict[str, Any],
    *,
    interval: float,
    calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Передаёт дальше фактические настройки и план; останавливается на ошибке."""

    preferences = case.preferences.model_copy(deep=True)
    plan = suite.plans[case.current_plan].model_copy(deep=True)
    context = suite.contexts[case.context].model_copy(deep=True)
    result["steps"] = []
    result["checks"] = {}

    def record_checks(turn: dict[str, Any]) -> bool:
        result["checks"].update(
            {
                f"step:{turn['step']}:{key}": value
                for key, value in turn["checks"].items()
            }
        )
        return all(turn["checks"].values())

    for index, step in enumerate(case.steps, start=1):
        if index > 1:
            print(f"Пауза перед следующей правкой: {interval:g} с.", flush=True)
            await asyncio.sleep(interval)
        result["failed_step"] = index
        turn = {
            "step": index,
            "user_message": step.user_message,
            "input_preferences": preferences.model_dump(mode="json"),
            "input_plan": plan.model_dump(mode="json"),
            "expectations": step.expectations.model_dump(mode="json"),
            "checks": {},
        }
        result["steps"].append(turn)
        first_call = len(calls)
        started = perf_counter()
        try:
            analysis = await ai.analyze_trip_edit(
                current_preferences=preferences,
                current_plan=plan,
                instruction=step.user_message,
            )
            turn["analysis"] = analysis.model_dump(mode="json")
            turn["checks"] = {
                "edit_supported": analysis.supported,
                "rest_changes": expanded_rest_changes(
                    analysis.rest_period_changes, preferences.duration_days
                )
                == expanded_rest_changes(
                    step.expectations.rest_period_changes, preferences.duration_days
                ),
            }
            expected_scope = step.expectations.regeneration_periods
            if expected_scope is not None:
                turn["checks"]["regeneration_scope"] = expanded_period_scopes(
                    analysis.regeneration_periods, preferences.duration_days
                ) == expanded_period_scopes(expected_scope, preferences.duration_days)
                turn["checks"]["regeneration_required"] = (
                    analysis.requires_regeneration == bool(expected_scope)
                )
            if not record_checks(turn):
                return finish_checks(result)
            try:
                updated = merge_edit_preferences(preferences, analysis)
            except ValidationError:
                turn["checks"]["valid_preferences"] = False
                record_checks(turn)
                return finish_checks(result)
            turn["updated_preferences"] = updated.model_dump(mode="json")
            turn["checks"]["rest_preferences"] = expanded_period_scopes(
                updated.rest_periods, preferences.duration_days
            ) == expanded_period_scopes(
                step.expectations.rest_periods, preferences.duration_days
            )
            turn["checks"]["required_preferences"] = Counter(
                normalize_text(name) for name in updated.must_visit_places
            ) == Counter(
                normalize_text(name) for name in case.preferences.must_visit_places
            )
            if not record_checks(turn):
                return finish_checks(result)
            generated = await ai.generate_edited_trip_plan(
                preferences=updated,
                current_preferences=preferences,
                travel_context=context,
                current_plan=plan,
                instruction=step.user_message,
                analysis=analysis,
            )
            assessment = assess_plan(generated, case, context)
            turn["checks"].update(assessment.pop("checks"))
            turn.update(assessment)
            turn["checks"].update(
                assess_edit_periods(generated, case, step.expectations, context)
            )
            turn["plan"] = generated.model_dump(mode="json")
            if not record_checks(turn):
                return finish_checks(result)
            preferences, plan = updated, generated
            print(f"{case.id}: правка {index}/{len(case.steps)} прошла.", flush=True)
        finally:
            turn["calls"] = calls[first_call:]
            turn["duration_ms"] = round((perf_counter() - started) * 1000)
    result.pop("failed_step", None)
    return finish_checks(result)


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
        call = {key: event[key] for key in allowed if key in event}
        for key in (
            "request_body_bytes",
            "provider_token_limit",
            "provider_tokens_remaining",
            "retry_after_ms",
        ):
            value = event.get(key)
            if type(value) is int and value >= 0:
                call[key] = value
        self.calls.append(call)


async def evaluate_case(
    case: EvaluationCase | IntakeEvaluationCase | EditEvaluationCase,
    suite: EvaluationSuite | IntakeEvaluationSuite | EditEvaluationSuite,
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
    elif isinstance(case, EditEvaluationCase):
        result["input_preferences"] = case.preferences.model_dump(mode="json")
        result["preserved_visits"] = [
            visit.model_dump(mode="json") for visit in case.preserved_visits
        ]
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
        if isinstance(case, EditEvaluationCase):
            return await evaluate_edit_dialogue(
                case, suite, result, interval=turn_interval, calls=collector.calls
            )
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
            current_preferences = preferences
            preferences = merge_edit_preferences(preferences, analysis)
            result["updated_preferences"] = preferences.model_dump(mode="json")
            plan = await ai.generate_edited_trip_plan(
                preferences=preferences,
                current_preferences=current_preferences,
                travel_context=context,
                current_plan=current_plan,
                instruction=case.user_message,
                analysis=analysis,
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
    cases: list[EvaluationCase] | list[IntakeEvaluationCase] | list[EditEvaluationCase],
    suite: EvaluationSuite | IntakeEvaluationSuite | EditEvaluationSuite,
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
            if isinstance(case, (IntakeEvaluationCase, EditEvaluationCase)):
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
    parser.add_argument(
        "--suite", choices=("routes", "intake", "edits"), default="routes"
    )
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
    cases_path, loader = {
        "routes": (CASES_PATH, load_suite),
        "intake": (INTAKE_CASES_PATH, load_intake_suite),
        "edits": (EDIT_CASES_PATH, load_edit_suite),
    }[args.suite]
    try:
        suite = loader()
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
            elif isinstance(case, EditEvaluationCase):
                print(f"{case.id}: edits | правок: {len(case.steps)}")
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
        if isinstance(suite, (IntakeEvaluationSuite, EditEvaluationSuite)):
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
