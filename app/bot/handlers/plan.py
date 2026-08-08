"""
Обработчик создания маршрута поездки.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from pydantic import ValidationError

from app.bot.api_client import BackendError, create_trip_plan
from app.bot.services.trip_formatter import (
    format_trip_plan,
    split_text,
)
from app.bot.states import TripPlanning
from app.schemas.trip import TripPreferences

# Router хранит обработчики планирования поездки.
router = Router()


def parse_duration_days(value: str) -> int | None:
    """Преобразует целое количество дней от 1 до 30."""

    normalized_value = value.strip()

    if not normalized_value.isdecimal() or len(normalized_value) > 2:
        return None

    duration_days = int(normalized_value)

    if not 1 <= duration_days <= 30:
        return None

    return duration_days


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
        "Куда хотите поехать?\n\nНапример: Япония, Стамбул или Италия."
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
        await message.answer("Напишите направление чуть подробнее.")
        return

    # Сохраняем ответ пользователя в Redis.
    await state.update_data(
        destination=destination,
    )

    # Переводим диалог на следующий этап.
    await state.set_state(TripPlanning.duration)

    await message.answer(
        "На сколько дней планируете поездку?\n\n"
        "Отправьте число от 1 до 30. Например: 7."
    )


@router.message(TripPlanning.destination)
async def destination_invalid_handler(
    message: Message,
) -> None:
    """
    Просит отправить направление обычным текстом.
    """

    await message.answer("Напишите направление текстом.\n\nНапример: Япония.")


@router.message(TripPlanning.duration, F.text)
async def duration_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Проверяет и сохраняет длительность поездки.
    """

    duration_days = parse_duration_days(message.text)

    if duration_days is None:
        await message.answer(
            "Отправьте количество дней числом от 1 до 30.\n\nНапример: 7."
        )
        return

    await state.update_data(
        duration_days=duration_days,
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
    Просит отправить допустимое количество дней.
    """

    await message.answer("Отправьте количество дней числом от 1 до 30.")


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
        "Что вам особенно интересно?\n\nНапример:\nМузеи, природа, еда, архитектура."
    )


@router.message(TripPlanning.budget)
async def budget_invalid_handler(
    message: Message,
) -> None:
    """
    Просит отправить бюджет текстом.
    """

    await message.answer("Напишите бюджет обычным текстом.")


@router.message(TripPlanning.interests, F.text)
async def interests_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Проверяет параметры, генерирует и показывает маршрут.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await state.clear()

        await message.answer("Не удалось определить пользователя Telegram.")
        return

    interests = message.text.strip()

    if not 2 <= len(interests) <= 1000:
        await message.answer("Опишите интересы длиной от 2 до 1000 символов.")
        return

    data = await state.get_data()

    try:
        preferences = TripPreferences.model_validate(
            {
                **data,
                "interests": interests,
            }
        )

    except ValidationError:
        await state.clear()

        await message.answer(
            "Параметры поездки заполнены неверно. Отправьте /plan и попробуйте ещё раз."
        )
        return

    await message.answer("✈️ Генерирую и сохраняю маршрут...")

    try:
        trip_plan = await create_trip_plan(
            telegram_id=telegram_user.id,
            first_name=telegram_user.first_name,
            preferences=preferences,
        )

    except BackendError as error:
        await state.clear()

        await message.answer(f"Не удалось создать маршрут:\n{error}")
        return

    formatted_plan = format_trip_plan(trip_plan)

    for text_part in split_text(formatted_plan):
        await message.answer(text_part)

    await state.clear()
