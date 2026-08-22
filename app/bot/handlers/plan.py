"""
Разговорный обработчик планирования поездки.
"""

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from pydantic import ValidationError

from app.bot.api_client import (
    BackendError,
    create_trip_plan,
    process_trip_intake,
)
from app.bot.handlers.history import send_trip_history
from app.bot.keyboards import (
    CANCEL_BUTTON_TEXT,
    MY_TRIPS_BUTTON_TEXT,
    NEW_TRIP_BUTTON_TEXT,
)
from app.bot.services.trip_formatter import (
    format_trip_plan,
    split_text,
)
from app.bot.states import TripPlanning
from app.schemas.trip import (
    TripDraft,
    TripIntakeResponse,
    TripPreferences,
)

logger = logging.getLogger(__name__)

router = Router()

PLAN_START_MESSAGE = (
    "Опишите желаемую поездку обычным сообщением.\n\n"
    "Например:\n"
    "Хочу осенью на неделю в Японию. "
    "Люблю современную архитектуру и местную еду."
)

UNKNOWN_REQUEST_MESSAGE = (
    "Я пока умею планировать поездки и показывать сохранённые маршруты.\n\n"
    "Напишите, куда и на сколько дней хотите поехать."
)


def restore_trip_draft(
    state_data: dict[str, Any],
) -> TripDraft:
    """
    Восстанавливает строгий TripDraft из данных Redis.

    FSM storage возвращает обычный словарь. Перед использованием
    мы снова проверяем его через Pydantic.
    """

    draft_data = state_data.get("draft", {})

    return TripDraft.model_validate(draft_data)


async def handle_trip_message(
    *,
    message: Message,
    state: FSMContext,
    user_message: str,
) -> None:
    """
    Обрабатывает одно свободное сообщение пользователя.

    Функция связывает Telegram, Redis, conversational intake
    и генерацию итогового маршрута.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await state.clear()
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    state_data = await state.get_data()

    try:
        current_draft = restore_trip_draft(state_data)

    except ValidationError:
        await state.clear()

        await message.answer(
            "Сохранённый черновик поездки повреждён, поэтому я его очистила.\n\n"
            "Опишите поездку ещё раз."
        )
        return

    try:
        intake_response = await process_trip_intake(
            telegram_id=telegram_user.id,
            user_message=user_message,
            draft=current_draft,
        )

    except BackendError as error:
        await message.answer(f"Не удалось разобрать сообщение:\n{error}")
        return

    await handle_intake_response(
        message=message,
        state=state,
        intake_response=intake_response,
    )


async def handle_intake_response(
    *,
    message: Message,
    state: FSMContext,
    intake_response: TripIntakeResponse,
) -> None:
    """
    Выполняет действие, выбранное conversational intake.
    """

    if intake_response.intent == "cancel":
        await state.clear()
        await message.answer("Планирование поездки отменено.")
        return

    if intake_response.intent == "show_trips":
        await state.clear()
        await send_trip_history(message)
        return

    if intake_response.intent == "unknown":
        await message.answer(UNKNOWN_REQUEST_MESSAGE)
        return

    # Для plan_trip сохраняем весь обновлённый черновик в Redis.
    await state.set_state(TripPlanning.collecting)

    await state.update_data(
        draft=intake_response.draft.model_dump(mode="json"),
    )

    if not intake_response.ready_to_generate:
        next_question = (
            intake_response.next_question or "Расскажите немного подробнее о поездке."
        )

        await message.answer(next_question)
        return

    try:
        preferences = TripPreferences.model_validate(
            intake_response.draft.model_dump(mode="python")
        )

    except ValidationError:
        await state.clear()

        await message.answer(
            "Не удалось проверить собранные параметры поездки.\n\n"
            "Опишите поездку ещё раз."
        )
        return

    await generate_and_send_trip(
        message=message,
        state=state,
        preferences=preferences,
    )


async def delete_progress_message(
    progress_message: Message,
) -> None:
    """
    Удаляет служебное сообщение о генерации маршрута.

    Ошибка Telegram не должна превращать успешно созданный маршрут
    в ошибку для пользователя.
    """

    try:
        await progress_message.delete()

    except TelegramAPIError as error:
        logger.warning(
            "Failed to delete trip generation progress message | error_type=%s",
            type(error).__name__,
        )


async def generate_and_send_trip(
    *,
    message: Message,
    state: FSMContext,
    preferences: TripPreferences,
) -> None:
    """
    Генерирует, сохраняет и отправляет готовый маршрут.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await state.clear()
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    progress_message = await message.answer("✈️ Генерирую и сохраняю маршрут...")

    try:
        try:
            trip_plan = await create_trip_plan(
                telegram_id=telegram_user.id,
                first_name=telegram_user.first_name,
                preferences=preferences,
            )

        except BackendError as error:
            # Черновик сохраняем, чтобы пользователь не вводил всё заново.
            await message.answer(
                f"Не удалось создать маршрут:\n{error}\n\n"
                "Черновик сохранён — сообщение можно отправить ещё раз."
            )
            return

        formatted_plan = format_trip_plan(trip_plan)

        for text_part in split_text(formatted_plan):
            await message.answer(text_part)

        await state.clear()

    finally:
        await delete_progress_message(progress_message)


@router.message(Command("plan"))
async def start_new_trip_dialog(
    *,
    message: Message,
    state: FSMContext,
) -> None:
    """
    Очищает старый черновик и начинает новую поездку.
    """

    await state.clear()
    await state.set_state(TripPlanning.collecting)

    await state.update_data(
        draft=TripDraft().model_dump(mode="json"),
    )

    await message.answer(PLAN_START_MESSAGE)


async def cancel_trip_dialog(
    *,
    message: Message,
    state: FSMContext,
) -> None:
    """
    Очищает активный черновик поездки.
    """

    await state.clear()
    await message.answer("Планирование поездки отменено.")


@router.message(Command("plan"))
async def plan_handler(
    message: Message,
    state: FSMContext,
    command: CommandObject,
) -> None:
    """
    Начинает новый диалог.

    Команда остаётся для совместимости, но больше не обязательна.
    """

    command_text = command.args.strip() if command.args else ""

    if command_text:
        await state.clear()

        await handle_trip_message(
            message=message,
            state=state,
            user_message=command_text,
        )
        return

    await start_new_trip_dialog(
        message=message,
        state=state,
    )


@router.message(Command("cancel"))
async def cancel_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Отменяет текущий диалог по команде.
    """

    await cancel_trip_dialog(
        message=message,
        state=state,
    )


@router.message(F.text == NEW_TRIP_BUTTON_TEXT)
async def new_trip_button_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Начинает новую поездку по кнопке главного меню.
    """

    await start_new_trip_dialog(
        message=message,
        state=state,
    )


@router.message(F.text == MY_TRIPS_BUTTON_TEXT)
async def trips_button_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Открывает историю без обращения к conversational intake.
    """

    await state.clear()
    await send_trip_history(message)


@router.message(F.text == CANCEL_BUTTON_TEXT)
async def cancel_button_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Отменяет поездку по кнопке главного меню.
    """

    await cancel_trip_dialog(
        message=message,
        state=state,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def free_text_handler(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Принимает свободный текст даже без команды /plan.
    """

    if message.text is None:
        return

    await handle_trip_message(
        message=message,
        state=state,
        user_message=message.text,
    )


@router.message(TripPlanning.collecting, ~F.text)
async def non_text_planning_handler(
    message: Message,
) -> None:
    """
    Просит использовать текст во время планирования.
    """

    await message.answer(
        "Пока я понимаю параметры поездки только из текста.\n\n"
        "Напишите сообщение обычным текстом."
    )
