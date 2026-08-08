"""
Форматирование маршрутов для Telegram.
"""

from typing import Any


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
        f"📅 Продолжительность: {duration_days} дней",
        "",
        "📝 Кратко",
        summary,
        "",
    ]

    for day in days:
        lines.append(f"📍 День {day['day']}: {day['title']}")
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
