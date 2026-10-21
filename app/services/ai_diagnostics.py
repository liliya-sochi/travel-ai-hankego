"""Безопасные причины отказа анализатора без ответов модели и входных значений."""

from typing import Any, Literal

from pydantic import ValidationError

from app.schemas.trip import TripEditAnalysis, TripIntakeExtraction

AnalysisValidationStage = Literal["extraction", "schema", "semantic"]
MAX_VALIDATION_ISSUES = 5
KNOWN_FIELDS = (
    frozenset(TripEditAnalysis.model_fields)
    | frozenset(TripIntakeExtraction.model_fields)
    | {"day", "period", "place_name", "activity_focus", "nearby", "action", "rest"}
)
KNOWN_ERROR_TYPES = frozenset(
    {
        "missing",
        "value_error",
        "json_invalid",
        "extra_forbidden",
        "literal_error",
        "bool_type",
        "int_type",
        "string_type",
        "list_type",
        "model_type",
        "string_too_short",
        "string_too_long",
        "too_long",
        "too_short",
        "greater_than_equal",
        "less_than_equal",
    }
)
KNOWN_VALIDATION_REASONS = {
    "Режим генерации должен соответствовать её периодам.": "regeneration_mode_mismatch",
    "Изменение интересов, замена или изменение обязательных мест без локальных посещений требует генерации.": "generation_required",
    "Удаляемые посещения не должны повторяться.": "duplicate_removals",
    "Removal day exceeds trip duration.": "removal_day_outside_trip",
    "Removal validation needs the saved plan.": "removal_plan_unavailable",
    "Removed visit must be explicitly named.": "removal_not_explicit",
    "Removed visit is still required.": "removed_visit_still_required",
    "Removal conflicts with another edit in the same period.": "removal_scope_conflict",
    "Removal does not identify one saved visit.": "removal_reference_not_unique",
    "Removal identifies the same saved visit twice.": "duplicate_removal_reference",
    "Добавляемые посещения не должны повторяться.": "duplicate_additions",
    "Заменяемые посещения не должны повторяться.": "duplicate_replacements",
    "Периоды генерации не должны повторяться.": "duplicate_regeneration_periods",
    "Действия одного периода не должны повторяться.": "duplicate_period_actions",
    "Изменения одного периода отдыха не должны повторяться.": "duplicate_rest_changes",
    "LLM returned a rest day outside the trip.": "rest_day_outside_trip",
    "Edit action day exceeds trip duration.": "action_day_outside_trip",
    "Regeneration day exceeds trip duration.": "regeneration_day_outside_trip",
    "Addition day exceeds trip duration.": "addition_day_outside_trip",
    "Replacement validation needs the saved plan.": "replacement_plan_unavailable",
    "LLM returned duplicate required places.": "duplicate_required_places",
    "LLM changed required places without declaring it.": "undeclared_required_change",
    "LLM changed a required place absent from the instruction.": "required_change_absent_from_instruction",
    "Added visit must be explicit and required.": "addition_not_explicit_or_required",
    "Local edit changes required places beyond declared visits.": "local_required_change_beyond_visits",
    "Replacement conflicts with generation scope or period action.": "replacement_scope_conflict",
    "Replacement does not identify one saved visit.": "replacement_reference_not_unique",
    "Replacement identifies the same saved visit twice.": "duplicate_replacement_reference",
}
SAFE_REASONS = frozenset(KNOWN_VALIDATION_REASONS.values()) | {
    "schema_validation",
    "model_text_unavailable",
    "other_analysis_validation",
}


def _safe_field_path(location: tuple[Any, ...]) -> str:
    """Оставляет только известные поля; номера элементов заменяет на []."""

    parts = [
        "[]"
        if type(part) is int
        else part
        if isinstance(part, str) and part in KNOWN_FIELDS
        else "<other>"
        for part in location[:6]
    ]
    return ".".join(parts).replace(".[]", "[]") if parts else "<root>"


def safe_analysis_validation_details(details: object) -> dict[str, Any]:
    """Повторно ограничивает поля при записи llm_call и сборе eval-отчёта."""

    if not isinstance(details, dict):
        return {}
    stage = details.get("validation_stage")
    stage = stage if stage in ("extraction", "schema", "semantic") else "unknown"
    reason = details.get("validation_reason")
    reason = (
        reason
        if isinstance(reason, str) and reason in SAFE_REASONS
        else "other_analysis_validation"
    )
    issues = []
    raw_issues = details.get("validation_issues")
    for issue in raw_issues[:20] if isinstance(raw_issues, (list, tuple)) else ():
        if not isinstance(issue, dict):
            continue
        field = issue.get("field")
        if field == "<root>":
            path = "<root>"
        elif isinstance(field, str) and len(field) <= 180:
            path = _safe_field_path(
                tuple(
                    0 if part == "[]" else part
                    for part in field.replace("[]", ".[]").split(".")
                )
            )
        else:
            path = "<other>"
        kind = issue.get("error_type")
        kind = (
            kind
            if isinstance(kind, str) and kind in KNOWN_ERROR_TYPES
            else "validation_error"
        )
        issue_reason = issue.get("reason")
        issue_reason = (
            issue_reason
            if isinstance(issue_reason, str) and issue_reason in SAFE_REASONS
            else "schema_validation"
        )
        safe_issue = {"field": path, "error_type": kind, "reason": issue_reason}
        if safe_issue not in issues:
            issues.append(safe_issue)
        if len(issues) == MAX_VALIDATION_ISSUES:
            break
    return {
        "validation_stage": stage,
        "validation_reason": reason,
        "validation_issues": issues,
    }


def analysis_validation_details(
    error: Exception, *, stage: AnalysisValidationStage
) -> dict[str, Any]:
    """Классифицирует только известные проверки; произвольные сообщения скрывает."""

    issues = []
    if isinstance(error, ValidationError):
        for item in error.errors(
            include_input=False, include_context=True, include_url=False
        )[:20]:
            context = item.get("ctx")
            underlying = context.get("error") if isinstance(context, dict) else None
            issues.append(
                {
                    "field": _safe_field_path(item["loc"]),
                    "error_type": item["type"],
                    "reason": KNOWN_VALIDATION_REASONS.get(
                        str(underlying), "schema_validation"
                    ),
                }
            )
        reason = next(
            (
                issue["reason"]
                for issue in issues
                if issue["reason"] != "schema_validation"
            ),
            "schema_validation",
        )
    elif stage == "extraction":
        reason = "model_text_unavailable"
    else:
        reason = KNOWN_VALIDATION_REASONS.get(str(error), "other_analysis_validation")
    return safe_analysis_validation_details(
        {
            "validation_stage": stage,
            "validation_reason": reason,
            "validation_issues": issues,
        }
    )
