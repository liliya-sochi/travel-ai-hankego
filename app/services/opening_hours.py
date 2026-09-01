"""Безопасная обработка часов работы из данных OpenStreetMap."""

import re
from typing import Literal

DayPeriod = Literal["morning", "afternoon", "evening"]

ALL_DAY_PERIODS: tuple[DayPeriod, ...] = (
    "morning",
    "afternoon",
    "evening",
)

MIN_PERIOD_OVERLAP_MINUTES = 60

_DAY_PERIOD_MINUTES: dict[DayPeriod, tuple[int, int]] = {
    "morning": (6 * 60, 12 * 60),
    "afternoon": (12 * 60, 18 * 60),
    "evening": (18 * 60, 24 * 60),
}

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

_CLOCK_TIME_PATTERN = re.compile(r"^(?P<hour>\d{2}):(?P<minute>\d{2})$")


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


def infer_available_periods(
    opening_hours: str | None,
) -> tuple[DayPeriod, ...] | None:
    """
    Определяет допустимые периоды дня для планирования.

    ``None`` означает, что расписание отсутствует или содержит
    неподдерживаемый синтаксис. В этом случае планировщик не вводит
    ограничение, чтобы неполные внешние данные не исключили место.
    """

    if opening_hours is None:
        return None

    normalized_hours = opening_hours.strip()

    if not normalized_hours:
        return None

    if normalized_hours == "24/7":
        return ALL_DAY_PERIODS

    open_intervals: list[tuple[int, int]] = []

    for segment in normalized_hours.split(";"):
        normalized_segment = segment.strip()

        if _TIME_ONLY_PATTERN.fullmatch(normalized_segment):
            schedule = normalized_segment
        else:
            schedule_match = _DAY_SCHEDULE_PATTERN.fullmatch(normalized_segment)

            if schedule_match is None:
                return None

            schedule = schedule_match.group("schedule")

        if schedule == "off":
            continue

        for time_range in schedule.split(","):
            start_text, end_text = time_range.strip().split(
                "-",
                maxsplit=1,
            )
            start_minutes = _parse_clock_minutes(start_text)
            end_minutes = _parse_clock_minutes(end_text)

            if start_minutes is None or end_minutes is None:
                return None

            if start_minutes == end_minutes:
                return None

            if start_minutes < end_minutes:
                open_intervals.append((start_minutes, end_minutes))
                continue

            open_intervals.extend(
                [
                    (start_minutes, 24 * 60),
                    (0, end_minutes),
                ]
            )

    return tuple(
        period
        for period in ALL_DAY_PERIODS
        if any(
            _calculate_overlap_minutes(
                open_interval,
                _DAY_PERIOD_MINUTES[period],
            )
            >= MIN_PERIOD_OVERLAP_MINUTES
            for open_interval in open_intervals
        )
    )


def _parse_clock_minutes(value: str) -> int | None:
    """Преобразует корректное время OSM в минуты от начала суток."""

    match = _CLOCK_TIME_PATTERN.fullmatch(value)

    if match is None:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))

    if minute > 59:
        return None

    if hour == 24:
        return 24 * 60 if minute == 0 else None

    if hour > 23:
        return None

    return hour * 60 + minute


def _calculate_overlap_minutes(
    first: tuple[int, int],
    second: tuple[int, int],
) -> int:
    """Return the overlap duration between two intervals in minutes."""

    first_start, first_end = first
    second_start, second_end = second

    overlap_start = max(first_start, second_start)
    overlap_end = min(first_end, second_end)

    return max(0, overlap_end - overlap_start)
