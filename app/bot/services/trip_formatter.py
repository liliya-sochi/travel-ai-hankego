"""
Форматирование маршрутов для Telegram.
"""

import re
from typing import Any


def _format_duration(duration_days: int) -> str:
    remainder_100 = duration_days % 100
    remainder_10 = duration_days % 10

    if remainder_10 == 1 and remainder_100 != 11:
        unit = "день"
    elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
        unit = "дня"
    else:
        unit = "дней"

    return f"{duration_days} {unit}"


def _format_day_heading(
    *,
    day_number: int,
    title: str,
) -> str:
    duplicate_prefix_pattern = re.compile(
        rf"^день\s+{day_number}\b\s*(?:[-–—:]\s*)?",
        flags=re.IGNORECASE,
    )

    normalized_title = duplicate_prefix_pattern.sub(
        "",
        title,
        count=1,
    ).strip()

    if not normalized_title:
        return f"📍 День {day_number}"

    return f"📍 День {day_number}: {normalized_title}"


def format_trip_plan(
    trip_plan: dict[str, Any],
) -> str:
    """
    Превращает маршрут backend в читаемый текст.
    """

    destination = trip_plan["destination"]
    duration_days = trip_plan["duration_days"]
    summary = trip_plan["summary"]
    days = trip_plan["days"]
    practical_tips = trip_plan.get(
        "practical_tips",
        [],
    )

    lines = [
        f"✈️ {destination}",
        f"📅 Продолжительность: {_format_duration(duration_days)}",
        "",
        "📝 Кратко",
        summary,
        "",
    ]

    for day in days:
        lines.append(
            _format_day_heading(
                day_number=day["day"],
                title=day["title"],
            )
        )
        lines.append("")

        if day["morning"]:
            lines.append("🌅 Утро")

            for activity in day["morning"]:
                lines.append(f"• {activity}")

            lines.append("")

        if day["afternoon"]:
            lines.append("☀️ День")

            for activity in day["afternoon"]:
                lines.append(f"• {activity}")

            lines.append("")

        if day["evening"]:
            lines.append("🌙 Вечер")

            for activity in day["evening"]:
                lines.append(f"• {activity}")

            lines.append("")

    if practical_tips:
        lines.append("💡 Практические советы")

        for tip in practical_tips:
            lines.append(f"• {tip}")

    return "\n".join(lines)


def split_text(
    text: str,
    max_length: int = 3800,
) -> list[str]:
    """
    Разделяет длинный текст на сообщения Telegram.
    """

    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    current_part: list[str] = []
    current_length = 0

    for line in text.splitlines():
        line_length = len(line) + 1

        if current_part and current_length + line_length > max_length:
            parts.append("\n".join(current_part))

            current_part = []
            current_length = 0

        current_part.append(line)
        current_length += line_length

    if current_part:
        parts.append("\n".join(current_part))

    return parts
