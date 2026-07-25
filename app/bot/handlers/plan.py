"""
Обработчик создания маршрута поездки.
"""

from typing import Any

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot.api_client import BackendError, create_trip_plan


# Router хранит обработчики планирования поездки.
router = Router()


@router.message(Command("plan"))
async def plan_handler(
    message: Message,
    command: CommandObject,
) -> None:
    """
    Получает описание поездки после команды /plan,
    отправляет его в FastAPI и показывает готовый маршрут.
    """

    prompt = (command.args or "").strip()

    if len(prompt) < 10:
        await message.answer(
            "После /plan опиши поездку подробнее.\n\n"
            "Например:\n"
            "/plan Хочу на неделю в Японию. "
            "Люблю природу и современную архитектуру."
        )
        return

    await message.answer(
        "✈️ Готовлю маршрут. Это может занять несколько секунд..."
    )

    try:
        trip_plan = await create_trip_plan(prompt)

    except BackendError as error:
        await message.answer(
            f"Не удалось создать маршрут:\n{error}"
        )
        return

    formatted_plan = format_trip_plan(trip_plan)

    for text_part in split_text(formatted_plan):
        await message.answer(text_part)


def format_trip_plan(trip_plan: dict[str, Any]) -> str:
    """
    Превращает ответ backend в читаемый текст для Telegram.
    """

    destination = trip_plan["destination"]
    duration_days = trip_plan["duration_days"]
    summary = trip_plan["summary"]
    days = trip_plan["days"]
    practical_tips = trip_plan.get("practical_tips", [])

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

        for activity in day["activities"]:
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
    Разделяет длинный текст на несколько сообщений Telegram.
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