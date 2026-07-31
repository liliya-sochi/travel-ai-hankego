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
from app.bot.services.trip_prompt import build_trip_prompt


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


@router.message(TripPlanning.duration, F.text)
async def duration_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Сохраняет длительность поездки.
    """

    duration = message.text.strip()

    if len(duration) < 1:
        await message.answer(
            "Напишите количество дней."
        )
        return

    await state.update_data(
        duration=duration,
    )

    await state.set_state(
        TripPlanning.budget,
    )

    await message.answer(
        "Какой примерный бюджет поездки?\n\n"
        "Например:\n"
        "1000 €\n"
        "150000 ₽\n"
        "Без ограничений"
    )


@router.message(TripPlanning.duration)
async def duration_invalid_handler(
    message: Message,
) -> None:
    """
    Просит отправить длительность текстом.
    """

    await message.answer(
        "Напишите длительность поездки обычным текстом."
    )


@router.message(TripPlanning.budget, F.text)
async def budget_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Сохраняет бюджет поездки.
    """

    budget = message.text.strip()

    await state.update_data(
        budget=budget,
    )

    await state.set_state(
        TripPlanning.interests,
    )

    await message.answer(
        "Что вам особенно интересно?\n\n"
        "Например:\n"
        "Музеи, природа, еда, архитектура."
    )


@router.message(TripPlanning.budget)
async def budget_invalid_handler(
    message: Message,
) -> None:
    """
    Просит отправить бюджет текстом.
    """

    await message.answer(
        "Напишите бюджет обычным текстом."
    )


@router.message(TripPlanning.interests, F.text)
async def interests_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Генерирует, сохраняет и показывает маршрут.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await state.clear()

        await message.answer(
            "Не удалось определить пользователя Telegram."
        )
        return

    interests = message.text.strip()

    await state.update_data(
        interests=interests,
    )

    data = await state.get_data()

    prompt = build_trip_prompt(data)

    await message.answer(
        "✈️ Генерирую и сохраняю маршрут..."
    )

    try:
        trip_plan = await create_trip_plan(
            telegram_id=telegram_user.id,
            first_name=telegram_user.first_name,
            prompt=prompt,
        )

    except BackendError as error:
        await state.clear()

        await message.answer(
            f"Не удалось создать маршрут:\n{error}"
        )
        return

    formatted_plan = format_trip_plan(trip_plan)

    for text_part in split_text(formatted_plan):
        await message.answer(text_part)

    await state.clear()


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