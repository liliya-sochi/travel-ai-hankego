"""Безопасное форматирование часов работы из данных OpenStreetMap."""

import re

_DAY_NAMES = {
    "Mo": "пн",
    "Tu": "вт",
    "We": "ср",
    "Th": "чт",
    "Fr": "пт",
    "Sa": "сб",
    "Su": "вс",
    "PH": "праздничные дни",
}

_DAY_CODE_PATTERN = r"(?:Mo|Tu|We|Th|Fr|Sa|Su|PH)"
_DAY_GROUP_PATTERN = rf"{_DAY_CODE_PATTERN}(?:-{_DAY_CODE_PATTERN})?"
_DAY_LIST_PATTERN = (
    rf"{_DAY_GROUP_PATTERN}"
    rf"(?:\s*,\s*{_DAY_GROUP_PATTERN})*"
)

_TIME_RANGE_PATTERN = r"\d{2}:\d{2}-\d{2}:\d{2}"
_TIME_LIST_PATTERN = (
    rf"{_TIME_RANGE_PATTERN}"
    rf"(?:\s*,\s*{_TIME_RANGE_PATTERN})*"
)

_DAY_SCHEDULE_PATTERN = re.compile(
    rf"^(?P<days>{_DAY_LIST_PATTERN})\s+"
    rf"(?P<schedule>off|{_TIME_LIST_PATTERN})$"
)

_TIME_ONLY_PATTERN = re.compile(rf"^{_TIME_LIST_PATTERN}$")


def _format_days(days: str) -> str:
    """Переводит список дней недели из синтаксиса OSM."""

    formatted_groups: list[str] = []

    for group in days.split(","):
        normalized_group = group.strip()

        if "-" in normalized_group:
            start_day, end_day = normalized_group.split(
                "-",
                maxsplit=1,
            )
            formatted_groups.append(f"{_DAY_NAMES[start_day]}–{_DAY_NAMES[end_day]}")
        else:
            formatted_groups.append(_DAY_NAMES[normalized_group])

    return ", ".join(formatted_groups)


def _format_schedule(schedule: str) -> str:
    """Переводит время работы или признак закрытого дня."""

    if schedule == "off":
        return "закрыто"

    return schedule.replace("-", "–")


def format_opening_hours(opening_hours: str) -> str:
    """
    Переводит только простые и однозначные часы работы.

    Если выражение содержит неизвестный синтаксис OpenStreetMap,
    исходное значение возвращается без изменений.
    """

    normalized_hours = opening_hours.strip()

    if normalized_hours == "24/7":
        return "круглосуточно"

    formatted_segments: list[str] = []

    for segment in normalized_hours.split(";"):
        normalized_segment = segment.strip()

        if _TIME_ONLY_PATTERN.fullmatch(normalized_segment):
            formatted_segments.append(_format_schedule(normalized_segment))
            continue

        schedule_match = _DAY_SCHEDULE_PATTERN.fullmatch(normalized_segment)

        if schedule_match is None:
            return normalized_hours

        formatted_days = _format_days(schedule_match.group("days"))
        formatted_schedule = _format_schedule(schedule_match.group("schedule"))

        formatted_segments.append(f"{formatted_days}: {formatted_schedule}")

    return "; ".join(formatted_segments)
