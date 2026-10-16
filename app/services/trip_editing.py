"""Детерминированные изменения периодов и проверяемое сохранение посещений."""

from dataclasses import dataclass

from app.schemas.geoapify import TravelContext
from app.schemas.grounded_trip import (
    ACTIVITY_FOCUS_DESCRIPTIONS,
    GroundedActivity,
    GroundedActivityFocus,
)
from app.schemas.trip import (
    TripDayPeriod,
    TripEditAnalysis,
    TripPlanResponse,
    TripPreferences,
)
from app.services.place_matching import (
    normalize_place_name,
    required_place_name_matches,
)

PERIODS: tuple[TripDayPeriod, ...] = ("morning", "afternoon", "evening")
QUIET_WALK_DESCRIPTION = "Спокойная прогулка."
PeriodKey = tuple[int, TripDayPeriod]


@dataclass(frozen=True)
class VisitReplacementConstraint:
    """Какие прежние ID запрещены и сколько новых посещений требуется в периоде."""

    excluded_ids: frozenset[str]
    previous_ids: frozenset[str]
    minimum_new_visits: int


class PreservedActivityUnavailableError(ValueError):
    """Сохраняемое посещение не удалось однозначно связать со свежими данными."""


def period_action_map(
    analysis: TripEditAnalysis, duration: int
) -> dict[PeriodKey, str]:
    """Разворачивает общие действия; действие конкретного дня имеет приоритет."""

    result: dict[PeriodKey, str] = {}
    for item in sorted(analysis.period_actions, key=lambda item: item.day is not None):
        days = range(1, duration + 1) if item.day is None else [item.day]
        for day in days:
            if day > duration:
                raise ValueError("Edit action day exceeds trip duration.")
            result[day, item.period] = item.action
    return result


def cancelled_rest_cells(
    current: TripPreferences, updated: TripPreferences
) -> set[PeriodKey]:
    return {
        (day, period)
        for day in range(1, updated.duration_days + 1)
        for period in PERIODS
        if current.is_rest_period(day, period)
        and not updated.is_rest_period(day, period)
    }


def regeneration_cells(analysis: TripEditAnalysis, duration: int) -> set[PeriodKey]:
    """Разворачивает разрешённую область генерации, проверяя границы поездки."""

    cells: set[PeriodKey] = set()
    for item in analysis.regeneration_periods:
        days = range(1, duration + 1) if item.day is None else [item.day]
        for day in days:
            if day > duration:
                raise ValueError("Regeneration day exceeds trip duration.")
            cells.add((day, item.period))
    return cells


def preserved_cells(
    analysis: TripEditAnalysis, current: TripPreferences, updated: TripPreferences
) -> set[PeriodKey]:
    """Сохраняет все периоды вне разрешённой области генерации и локальной правки."""

    actions = period_action_map(analysis, updated.duration_days)
    cells = {
        (day, period)
        for day in range(1, updated.duration_days + 1)
        for period in PERIODS
    } - regeneration_cells(analysis, updated.duration_days)
    cells -= cancelled_rest_cells(current, updated)
    for cell, action in actions.items():
        if action == "preserve":
            cells.add(cell)
        else:
            cells.discard(cell)
    return {cell for cell in cells if not updated.is_rest_period(*cell)}


def requires_full_generation(
    analysis: TripEditAnalysis, current: TripPreferences, updated: TripPreferences
) -> bool:
    """Отмена отдыха без новой общей активности требует выбора новой активности."""

    walks = {
        cell
        for cell, action in period_action_map(analysis, updated.duration_days).items()
        if action == "quiet_walk"
    }
    return analysis.requires_regeneration or bool(
        cancelled_rest_cells(current, updated) - walks
    )


def saved_visit_reference(text: str) -> tuple[str, GroundedActivityFocus] | None:
    """Извлекает только название и тип из известного формата; справочный хвост не читает."""

    for focus, description in ACTIVITY_FOCUS_DESCRIPTIONS.items():
        marker = f": {description}."
        position = text.rfind(marker)
        if position > 0:
            suffix = text[position + len(marker) :]
            if not suffix or suffix.startswith(
                (
                    " Актуальный адрес по данным Google Maps: ",
                    " Часы по данным Geoapify: ",
                    " Часы по данным Google Maps: ",
                    " Сайт из данных Geoapify: ",
                    " Сайт из данных Google Maps: ",
                )
            ):
                return text[:position], focus
    return None


def preserved_place_names(plan: TripPlanResponse, cells: set[PeriodKey]) -> list[str]:
    """Передаёт имена сохраняемых посещений для проверки, не меняя must_visit_places."""

    names: dict[str, str] = {}
    for day in plan.days:
        for period in PERIODS:
            if (day.day, period) in cells:
                for text in getattr(day, period):
                    reference = saved_visit_reference(text)
                    if reference is not None:
                        names.setdefault(
                            normalize_place_name(reference[0]), reference[0]
                        )
    return list(names.values())


def replacement_visit_names(
    plan: TripPlanResponse, analysis: TripEditAnalysis
) -> dict[PeriodKey, set[str]]:
    """Проверяет, что анализ заменяет реальные посещения внутри области генерации."""

    allowed = regeneration_cells(analysis, plan.duration_days)
    actions = period_action_map(analysis, plan.duration_days)
    result: dict[PeriodKey, set[str]] = {}
    by_day = {day.day: day for day in plan.days}
    for replacement in analysis.place_replacements:
        cell = replacement.day, replacement.period
        if cell not in allowed or cell in actions:
            raise ValueError(
                "Replacement conflicts with generation scope or period action."
            )
        names = [
            reference[0]
            for line in getattr(by_day[replacement.day], replacement.period)
            if (reference := saved_visit_reference(line)) is not None
        ]
        exact = [
            name
            for name in names
            if normalize_place_name(name)
            == normalize_place_name(replacement.place_name)
        ]
        matches = exact or [
            name
            for name in names
            if required_place_name_matches(
                required_name=replacement.place_name, candidate_name=name
            )
        ]
        if len(matches) != 1:
            raise ValueError("Replacement does not identify one saved visit.")
        result.setdefault(cell, set()).add(matches[0])
    return result


def resolve_visit_replacements(
    plan: TripPlanResponse, analysis: TripEditAnalysis, context: TravelContext
) -> dict[PeriodKey, VisitReplacementConstraint]:
    """Связывает прежние названия со свежими ID; отсутствие старого места допустимо."""

    by_day = {day.day: day for day in plan.days}
    result: dict[PeriodKey, VisitReplacementConstraint] = {}

    def matching_ids(names: set[str]) -> frozenset[str]:
        return frozenset(
            place.source_place_id
            for name in names
            for place in context.places
            if normalize_place_name(name)
            in {
                normalize_place_name(place.name),
                normalize_place_name(place.display_name),
            }
            or required_place_name_matches(
                required_name=name, candidate_name=place.display_name
            )
        )

    for (day, period), names in replacement_visit_names(plan, analysis).items():
        previous_names = {
            reference[0]
            for line in getattr(by_day[day], period)
            if (reference := saved_visit_reference(line)) is not None
        }
        result[day, period] = VisitReplacementConstraint(
            excluded_ids=matching_ids(names),
            previous_ids=matching_ids(previous_names),
            minimum_new_visits=len(names),
        )
    return result


def resolve_preserved_activities(
    plan: TripPlanResponse, cells: set[PeriodKey], context: TravelContext
) -> dict[PeriodKey, list[GroundedActivity]]:
    """Связывает каждое прежнее посещение с единственным свежим кандидатом."""

    result: dict[PeriodKey, list[GroundedActivity]] = {}
    for day in plan.days:
        for period in PERIODS:
            cell = day.day, period
            if cell not in cells:
                continue
            activities: list[GroundedActivity] = []
            for text in getattr(day, period):
                reference = saved_visit_reference(text)
                if reference is None:
                    activities.append(
                        GroundedActivity(
                            source_place_id=None,
                            place_name=None,
                            activity_focus=None,
                            description=text,
                        )
                    )
                    continue
                name, focus = reference
                exact = [
                    place
                    for place in context.places
                    if normalize_place_name(name)
                    in {
                        normalize_place_name(place.name),
                        normalize_place_name(place.display_name),
                    }
                ]
                matches = exact or [
                    place
                    for place in context.places
                    if required_place_name_matches(
                        required_name=name, candidate_name=place.display_name
                    )
                ]
                if len(matches) != 1:
                    raise PreservedActivityUnavailableError(
                        "Saved visit is missing or ambiguous."
                    )
                place = matches[0]
                activities.append(
                    GroundedActivity(
                        source_place_id=place.source_place_id,
                        place_name=place.name,
                        activity_focus=focus,
                        description=None,
                    )
                )
            result[cell] = activities
    return result
