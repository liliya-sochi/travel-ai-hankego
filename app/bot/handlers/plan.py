"""
Обработчик создания маршрута поездки.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.api_client import BackendError, create_trip_plan
from app.bot.states import TripPlanning
from app.bot.services.trip_prompt import build_trip_prompt
from app.bot.services.trip_formatter import (
    format_trip_plan,
    split_text,
)


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