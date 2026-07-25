"""
Обработчик создания маршрута поездки.
"""

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.api_client import BackendError, create_trip_plan
from app.bot.states import TripPlanning


# Router хранит обработчики планирования поездки.
router = Router()


@router.message(Command("plan"))
async def plan_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Запускает пошаговое планирование поездки.
    """

    # Очищаем предыдущий незавершённый диалог,
    # если пользователь снова отправил /plan.
    await state.clear()

    # Устанавливаем состояние:
    # теперь бот ожидает направление поездки.
    await state.set_state(TripPlanning.destination)

    await message.answer(
        "Куда хотите поехать?\n\n"
        "Например: Япония, Стамбул или Италия."
    )


@router.message(TripPlanning.destination, F.text)
async def destination_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Сохраняет направление и спрашивает длительность поездки.
    """

    destination = message.text.strip()

    if len(destination) < 2:
        await message.answer(
            "Напишите направление чуть подробнее."
        )
        return

    # Сохраняем ответ пользователя в Redis.
    await state.update_data(
        destination=destination,
    )

    # Переводим диалог на следующий этап.
    await state.set_state(TripPlanning.duration)

    await message.answer(
        "На сколько дней планируете поездку?\n\n"
        "Например: 7 дней."
    )


@router.message(TripPlanning.destination)
async def destination_invalid_handler(
    message: Message,
) -> None:
    """
    Просит отправить направление обычным текстом.
    """

    await message.answer(
        "Напишите направление текстом.\n\n"
        "Например: Япония."
    )


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