"""Детерминированные изменения периодов и проверяемое сохранение посещений."""

from dataclasses import dataclass

from app.schemas.geoapify import TravelContext
from app.schemas.grounded_trip import (
    ACTIVITY_FOCUS_DESCRIPTIONS,
    REST_PERIOD_DESCRIPTION,
    GroundedActivity,
    GroundedActivityFocus,
    get_supported_activity_focuses,
)
from app.schemas.trip import (
    MAX_ACTIVITIES_PER_PERIOD,
    TripDayPeriod,
    TripEditAnalysis,
    TripPlaceReplacement,
    TripPlanResponse,
    TripPreferences,
)
from app.services.place_geography import calculate_distance_meters
from app.services.place_matching import (
    normalize_place_name,
    required_place_name_matches,
)

PERIODS: tuple[TripDayPeriod, ...] = ("morning", "afternoon", "evening")
QUIET_WALK_DESCRIPTION = "Спокойная прогулка."
PeriodKey = tuple[int, TripDayPeriod]
MAX_NEARBY_REPLACEMENT_DISTANCE_METERS = 2_000


@dataclass(frozen=True)
class ReplacementVisitRequirement:
    """Проверенные кандидаты для одного нового посещения и его запрошенный фокус."""

    activity_focus: GroundedActivityFocus | None
    nearby: bool
    allowed_ids: frozenset[str]


@dataclass(frozen=True)
class VisitReplacementConstraint:
    """Какие прежние ID запрещены и сколько новых посещений требуется в периоде."""

    excluded_ids: frozenset[str]
    previous_ids: frozenset[str]
    minimum_new_visits: int
    requirements: tuple[ReplacementVisitRequirement, ...]


class ReplacementReferenceUnavailableError(ValueError):
    """Свежие координаты прежнего места не удалось определить однозначно."""


def distinct_replacement_choices(choices: list[set[str]]) -> bool:
    """Проверяет, что для каждой замены есть своё подходящее новое место."""

    def assign(index: int, used: set[str]) -> bool:
        if index == len(choices):
            return True
        return any(
            assign(index + 1, used | {place_id}) for place_id in choices[index] - used
        )

    return assign(0, set())


class PreservedActivityUnavailableError(ValueError):
    """Сохраняемое посещение не удалось однозначно связать со свежими данными."""


class PlaceAdditionUnavailableError(ValueError):
    """Безопасное объяснение невозможности добавить посещение без потерь."""


class PlaceRemovalUnavailableError(ValueError):
    """Удаление оставило бы период пустым; нужен явный выбор пользователя."""


def addition_cells(analysis: TripEditAnalysis, duration: int) -> set[PeriodKey]:
    """Проверяет конкретные дни именованных добавлений."""

    if any(item.day > duration for item in analysis.place_additions):
        raise ValueError("Addition day exceeds trip duration.")
    return {(item.day, item.period) for item in analysis.place_additions}


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
        cancelled_rest_cells(current, updated)
        - walks
        - addition_cells(analysis, updated.duration_days)
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


def removed_visit_names(
    plan: TripPlanResponse, analysis: TripEditAnalysis
) -> dict[PeriodKey, set[str]]:
    """Находит удаляемые посещения в сохранённом плане, не требуя свежего места."""

    allowed_generation = regeneration_cells(analysis, plan.duration_days)
    actions = period_action_map(analysis, plan.duration_days)
    other_visits = {
        (item.day, item.period)
        for item in [*analysis.place_additions, *analysis.place_replacements]
    }
    by_day = {day.day: day for day in plan.days}
    result: dict[PeriodKey, set[str]] = {}
    for request in analysis.place_removals:
        cell = request.day, request.period
        if request.day not in by_day:
            raise ValueError("Removal day exceeds trip duration.")
        if (
            cell in allowed_generation
            or cell in other_visits
            or actions.get(cell) == "quiet_walk"
            or any(
                change.day in (None, request.day) and change.period == request.period
                for change in analysis.rest_period_changes
            )
        ):
            raise ValueError("Removal conflicts with another edit in the same period.")
        names = [
            reference[0]
            for line in getattr(by_day[request.day], request.period)
            if (reference := saved_visit_reference(line)) is not None
        ]
        exact = [
            name
            for name in names
            if normalize_place_name(name) == normalize_place_name(request.place_name)
        ]
        matches = exact or [
            name
            for name in names
            if required_place_name_matches(
                required_name=request.place_name, candidate_name=name
            )
        ]
        if len(matches) != 1:
            raise ValueError("Removal does not identify one saved visit.")
        removed = result.setdefault(cell, set())
        if matches[0] in removed:
            raise ValueError("Removal identifies the same saved visit twice.")
        removed.add(matches[0])
    return result


def plan_without_removed_visits(
    plan: TripPlanResponse, analysis: TripEditAnalysis
) -> TripPlanResponse:
    """Копирует план без указанных посещений, сохраняя порядок и текст остальных."""

    removed = removed_visit_names(plan, analysis)
    result = plan.model_copy(deep=True)
    for day in result.days:
        for period in PERIODS:
            names = removed.get((day.day, period), set())
            if not names:
                continue
            remaining = [
                line
                for line in getattr(day, period)
                if (reference := saved_visit_reference(line)) is None
                or reference[0] not in names
            ]
            if not remaining:
                raise PlaceRemovalUnavailableError(
                    "После удаления в периоде не останется активностей. "
                    "Укажите замену или попросите запланировать отдых. Маршрут не изменён."
                )
            setattr(day, period, remaining)
    return result


def context_without_removed_visits(
    context: TravelContext, removed: dict[PeriodKey, set[str]]
) -> TravelContext:
    """Не позволяет генератору вернуть удалённое место в другом периоде."""

    names = {name for values in removed.values() for name in values}
    if not names:
        return context
    places = [
        place
        for place in context.places
        if not any(
            normalize_place_name(name)
            in {
                normalize_place_name(place.name),
                normalize_place_name(place.display_name),
            }
            or required_place_name_matches(
                required_name=name, candidate_name=place.display_name
            )
            for name in names
        )
    ]
    return TravelContext.model_validate({**context.model_dump(), "places": places})


def _saved_replacement_requests(
    plan: TripPlanResponse, analysis: TripEditAnalysis
) -> list[tuple[TripPlaceReplacement, str]]:
    """Проверяет, что анализ заменяет реальные посещения внутри области генерации."""

    allowed = regeneration_cells(analysis, plan.duration_days)
    actions = period_action_map(analysis, plan.duration_days)
    result: list[tuple[TripPlaceReplacement, str]] = []
    seen: set[tuple[PeriodKey, str]] = set()
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
        key = cell, normalize_place_name(matches[0])
        if key in seen:
            raise ValueError("Replacement identifies the same saved visit twice.")
        seen.add(key)
        result.append((replacement, matches[0]))
    return result


def replacement_visit_names(
    plan: TripPlanResponse, analysis: TripEditAnalysis
) -> dict[PeriodKey, set[str]]:
    """Возвращает проверенные прежние названия с группировкой по периодам."""

    result: dict[PeriodKey, set[str]] = {}
    for request, name in _saved_replacement_requests(plan, analysis):
        result.setdefault((request.day, request.period), set()).add(name)
    return result


def replacement_reference_names(
    plan: TripPlanResponse, analysis: TripEditAnalysis
) -> list[str]:
    """Запрашивает свежие данные прежних мест только для замен поблизости."""

    return list(
        dict.fromkeys(
            name
            for request, name in _saved_replacement_requests(plan, analysis)
            if request.nearby
        )
    )


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

    requests: dict[PeriodKey, list[tuple[TripPlaceReplacement, str]]] = {}
    for request, name in _saved_replacement_requests(plan, analysis):
        requests.setdefault((request.day, request.period), []).append((request, name))
    for (day, period), items in requests.items():
        names = {name for _, name in items}
        previous_names = {
            reference[0]
            for line in getattr(by_day[day], period)
            if (reference := saved_visit_reference(line)) is not None
        }
        requirements: list[ReplacementVisitRequirement] = []
        for request, name in items:
            anchor = None
            if request.nearby:
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
                    raise ReplacementReferenceUnavailableError(
                        "Replacement reference is missing or ambiguous."
                    )
                anchor = matches[0]
            allowed = frozenset(
                place.source_place_id
                for place in context.places
                if (
                    request.activity_focus is None
                    or request.activity_focus in get_supported_activity_focuses(place)
                )
                and (
                    anchor is None
                    or calculate_distance_meters(
                        first_latitude=anchor.latitude,
                        first_longitude=anchor.longitude,
                        second_latitude=place.latitude,
                        second_longitude=place.longitude,
                    )
                    <= MAX_NEARBY_REPLACEMENT_DISTANCE_METERS
                )
            )
            requirements.append(
                ReplacementVisitRequirement(
                    activity_focus=request.activity_focus,
                    nearby=request.nearby,
                    allowed_ids=allowed,
                )
            )
        result[day, period] = VisitReplacementConstraint(
            excluded_ids=matching_ids(names),
            previous_ids=matching_ids(previous_names),
            minimum_new_visits=len(names),
            requirements=tuple(requirements),
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


def resolve_added_activities(
    *,
    plan: TripPlanResponse,
    analysis: TripEditAnalysis,
    context: TravelContext,
    current: TripPreferences,
    updated: TripPreferences,
    preserved: dict[PeriodKey, list[GroundedActivity]],
    preferred_focuses: list[GroundedActivityFocus],
) -> tuple[dict[PeriodKey, list[GroundedActivity]], int]:
    """Добавляет проверенные посещения к прежним, соблюдая вместимость периода."""

    cells = addition_cells(analysis, updated.duration_days)
    replacement_cells = {
        (item.day, item.period) for item in analysis.place_replacements
    }
    if cells.intersection(replacement_cells):
        raise PlaceAdditionUnavailableError(
            "Добавление и замену в одном периоде отправьте отдельными сообщениями. "
            "Маршрут не изменён."
        )
    if any(updated.is_rest_period(*cell) for cell in cells):
        raise PlaceAdditionUnavailableError(
            "В выбранном периоде запланирован отдых. Отмените его или выберите другое время. "
            "Маршрут не изменён."
        )
    result = resolve_preserved_activities(plan, cells - preserved.keys(), context)
    result.update(
        {
            cell: [activity.model_copy() for activity in preserved[cell]]
            for cell in cells & preserved.keys()
        }
    )
    for cell in cancelled_rest_cells(current, updated) & cells:
        result[cell] = [
            activity
            for activity in result[cell]
            if activity.description is None
            or " ".join(activity.description.casefold().split()).rstrip(".!…")
            != REST_PERIOD_DESCRIPTION.casefold().rstrip(".")
        ]
    added_count = 0
    for request in analysis.place_additions:
        cell = request.day, request.period
        exact = [
            place
            for place in context.places
            if normalize_place_name(request.place_name)
            in {
                normalize_place_name(place.name),
                normalize_place_name(place.display_name),
            }
        ]
        matches = exact or [
            place
            for place in context.places
            if required_place_name_matches(
                required_name=request.place_name, candidate_name=place.display_name
            )
        ]
        if len(matches) != 1:
            raise PlaceAdditionUnavailableError(
                "Не удалось однозначно подтвердить добавляемое место. "
                "Маршрут не изменён. Уточните название."
            )
        place = matches[0]
        supported = get_supported_activity_focuses(place)
        focus = request.activity_focus or next(
            (f for f in preferred_focuses if f in supported), None
        )
        if focus not in supported:
            raise PlaceAdditionUnavailableError(
                "Добавляемое место не подходит для указанного вида активности. Маршрут не изменён."
            )
        existing = next(
            (a for a in result[cell] if a.source_place_id == place.source_place_id),
            None,
        )
        if existing is not None:
            if (
                request.activity_focus is not None
                and existing.activity_focus != request.activity_focus
            ):
                raise PlaceAdditionUnavailableError(
                    "Это место уже включено в период с другим действием. "
                    "Уточните, нужно ли изменить посещение. Маршрут не изменён."
                )
            continue
        if len(result[cell]) >= MAX_ACTIVITIES_PER_PERIOD:
            raise PlaceAdditionUnavailableError(
                "В выбранном периоде уже две активности. Выберите другой период или замену. "
                "Маршрут не изменён."
            )
        result[cell].append(
            GroundedActivity(
                source_place_id=place.source_place_id,
                place_name=place.name,
                activity_focus=focus,
                description=None,
            )
        )
        added_count += 1
    return result, added_count
