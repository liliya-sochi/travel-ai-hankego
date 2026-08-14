"""
Обработчики сохранённых маршрутов.
"""

from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.api_client import (
    BackendError,
    delete_trip,
    get_trip_details,
    get_trip_history,
)
from app.bot.services.trip_formatter import (
    format_trip_plan,
    split_text,
)

router = Router()

OPEN_TRIP_PREFIX = "trip_open:"
DELETE_REQUEST_PREFIX = "trip_delete_request:"
DELETE_CONFIRM_PREFIX = "trip_delete_confirm:"
DELETE_CANCEL_PREFIX = "trip_delete_cancel:"


def build_trip_history_keyboard(
    trips: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    """
    Создаёт кнопки открытия и удаления для списка маршрутов.
    """

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    for index, trip in enumerate(
        trips,
        start=1,
    ):
        trip_id = trip["trip_id"]

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"📍 Открыть {index}",
                    callback_data=f"{OPEN_TRIP_PREFIX}{trip_id}",
                ),
                InlineKeyboardButton(
                    text=f"🗑 Удалить {index}",
                    callback_data=f"{DELETE_REQUEST_PREFIX}{trip_id}",
                ),
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=keyboard_rows,
    )


def build_trip_actions_keyboard(
    trip_id: int,
) -> InlineKeyboardMarkup:
    """
    Создаёт действия для открытого маршрута.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить маршрут",
                    callback_data=f"{DELETE_REQUEST_PREFIX}{trip_id}",
                ),
            ]
        ]
    )


def build_delete_confirmation_keyboard(
    trip_id: int,
) -> InlineKeyboardMarkup:
    """
    Создаёт безопасное подтверждение удаления маршрута.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"{DELETE_CONFIRM_PREFIX}{trip_id}",
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"{DELETE_CANCEL_PREFIX}{trip_id}",
                ),
            ]
        ]
    )


async def send_trip_history(
    message: Message,
) -> None:
    """
    Загружает и показывает последние маршруты пользователя.

    Функцию можно вызвать как из команды /trips,
    так и после распознавания намерения show_trips.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    try:
        history = await get_trip_history(
            telegram_id=telegram_user.id,
            limit=10,
        )

    except BackendError as error:
        await message.answer(f"Не удалось загрузить маршруты:\n{error}")
        return

    trips = history.get("trips", [])

    if not trips:
        await message.answer(
            "У вас пока нет сохранённых маршрутов.\n\n"
            "Просто опишите поездку, которую хотите запланировать."
        )
        return

    await message.answer(
        format_trip_history(trips),
        reply_markup=build_trip_history_keyboard(trips),
    )


@router.message(Command("trips"))
async def trips_handler(
    message: Message,
) -> None:
    """
    Показывает последние маршруты по команде /trips.
    """

    await send_trip_history(message)


async def send_trip_details(
    *,
    message: Message,
    telegram_id: int,
    trip_id: int,
) -> None:
    """
    Загружает и отправляет один сохранённый маршрут.
    """

    try:
        trip = await get_trip_details(
            telegram_id=telegram_id,
            trip_id=trip_id,
        )

    except BackendError as error:
        await message.answer(f"Не удалось загрузить маршрут:\n{error}")
        return

    formatted_trip = format_trip_plan(trip)
    text_parts = split_text(formatted_trip)

    for index, text_part in enumerate(text_parts):
        is_last_part = index == len(text_parts) - 1

        reply_markup = build_trip_actions_keyboard(trip_id) if is_last_part else None

        await message.answer(
            text_part,
            reply_markup=reply_markup,
        )


@router.message(Command("trip"))
async def trip_handler(
    message: Message,
) -> None:
    """
    Показывает маршрут по старой команде.

    Команда сохраняется для обратной совместимости.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    trip_id = extract_trip_id(message.text)

    if trip_id is None:
        await message.answer(
            "Не удалось определить ID маршрута.\n\n"
            "Откройте «🧳 Мои маршруты» и выберите нужный кнопкой."
        )
        return

    await send_trip_details(
        message=message,
        telegram_id=telegram_user.id,
        trip_id=trip_id,
    )


@router.callback_query(F.data.startswith(OPEN_TRIP_PREFIX))
async def open_trip_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Открывает маршрут по inline-кнопке.
    """

    trip_id = extract_callback_trip_id(
        callback.data,
        OPEN_TRIP_PREFIX,
    )

    if trip_id is None:
        await callback.answer(
            "Некорректный ID маршрута.",
            show_alert=True,
        )
        return

    # Убираем индикатор ожидания на нажатой Telegram-кнопке.
    await callback.answer()

    if not isinstance(callback.message, Message):
        await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text="Не удалось открыть сообщение со списком маршрутов.",
        )
        return

    await send_trip_details(
        message=callback.message,
        telegram_id=callback.from_user.id,
        trip_id=trip_id,
    )


async def send_delete_confirmation(
    *,
    message: Message,
    telegram_id: int,
    trip_id: int,
) -> None:
    """
    Загружает маршрут и запрашивает подтверждение удаления.
    """

    try:
        trip = await get_trip_details(
            telegram_id=telegram_id,
            trip_id=trip_id,
        )

    except BackendError as error:
        await message.answer(f"Не удалось загрузить маршрут:\n{error}")
        return

    await message.answer(
        f"Удалить маршрут «{trip['destination']}»?\n\nЭто действие нельзя отменить.",
        reply_markup=build_delete_confirmation_keyboard(trip_id),
    )


@router.message(Command("delete_trip"))
async def delete_trip_handler(
    message: Message,
) -> None:
    """
    Запрашивает удаление по старой команде.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await message.answer("Не удалось определить пользователя Telegram.")
        return

    trip_id = extract_trip_id(message.text)

    if trip_id is None:
        await message.answer(
            "Не удалось определить ID маршрута.\n\n"
            "Откройте «🧳 Мои маршруты» и выберите удаление кнопкой."
        )
        return

    await send_delete_confirmation(
        message=message,
        telegram_id=telegram_user.id,
        trip_id=trip_id,
    )


@router.callback_query(F.data.startswith(DELETE_REQUEST_PREFIX))
async def request_trip_delete_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Запрашивает подтверждение удаления по inline-кнопке.
    """

    trip_id = extract_callback_trip_id(
        callback.data,
        DELETE_REQUEST_PREFIX,
    )

    if trip_id is None:
        await callback.answer(
            "Некорректный ID маршрута.",
            show_alert=True,
        )
        return

    await callback.answer()

    if not isinstance(callback.message, Message):
        await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text="Не удалось открыть сообщение со списком маршрутов.",
        )
        return

    await send_delete_confirmation(
        message=callback.message,
        telegram_id=callback.from_user.id,
        trip_id=trip_id,
    )


@router.callback_query(F.data.startswith(DELETE_CONFIRM_PREFIX))
async def confirm_trip_delete_handler(
    callback: CallbackQuery,
) -> None:
    """
    Удаляет маршрут после подтверждения пользователя.
    """

    trip_id = extract_callback_trip_id(
        callback.data,
        DELETE_CONFIRM_PREFIX,
    )

    if trip_id is None:
        await callback.answer(
            "Некорректный ID маршрута.",
            show_alert=True,
        )
        return

    # Сразу убираем индикатор загрузки Telegram.
    await callback.answer()

    try:
        result = await delete_trip(
            telegram_id=callback.from_user.id,
            trip_id=trip_id,
        )

    except BackendError as error:
        await replace_callback_message(
            callback,
            (f"Не удалось удалить маршрут:\n{error}"),
        )
        return

    await replace_callback_message(
        callback,
        (
            f"Маршрут удалён.\n\n"
            f"ID: {result['trip_id']}\n"
            f"Оставшиеся маршруты можно открыть кнопкой «🧳 Мои маршруты»."
        ),
    )


@router.callback_query(F.data.startswith(DELETE_CANCEL_PREFIX))
async def cancel_trip_delete_handler(
    callback: CallbackQuery,
) -> None:
    """
    Отменяет удаление маршрута.
    """

    trip_id = extract_callback_trip_id(
        callback.data,
        DELETE_CANCEL_PREFIX,
    )

    if trip_id is None:
        await callback.answer(
            "Некорректный ID маршрута.",
            show_alert=True,
        )
        return

    await callback.answer("Удаление отменено.")

    await replace_callback_message(
        callback,
        "Удаление маршрута отменено.",
        reply_markup=build_trip_actions_keyboard(trip_id),
    )


async def replace_callback_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Заменяет сообщение с inline-кнопками.

    Если исходное сообщение недоступно,
    отправляет пользователю новое.
    """

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text=text,
            reply_markup=reply_markup,
        )
        return

    await callback.bot.send_message(
        chat_id=callback.from_user.id,
        text=text,
        reply_markup=reply_markup,
    )


def extract_trip_id(
    message_text: str | None,
) -> int | None:
    """
    Извлекает положительный ID из команды.
    """

    if message_text is None:
        return None

    command_parts = message_text.split(maxsplit=1)

    if len(command_parts) != 2:
        return None

    raw_trip_id = command_parts[1].strip()

    if not raw_trip_id.isdigit():
        return None

    trip_id = int(raw_trip_id)

    if trip_id <= 0:
        return None

    return trip_id


def extract_callback_trip_id(
    callback_data: str | None,
    prefix: str,
) -> int | None:
    """
    Извлекает положительный ID из callback data.
    """

    if callback_data is None or not callback_data.startswith(prefix):
        return None

    raw_trip_id = callback_data.removeprefix(prefix)

    if not raw_trip_id.isdigit():
        return None

    trip_id = int(raw_trip_id)

    if trip_id <= 0:
        return None

    return trip_id


def format_trip_history(
    trips: list[dict[str, Any]],
) -> str:
    """
    Формирует читаемый список маршрутов.
    """

    lines = [
        "🧳 Ваши последние маршруты",
        "",
    ]

    for index, trip in enumerate(
        trips,
        start=1,
    ):
        created_at = format_created_at(str(trip["created_at"]))

        lines.extend(
            [
                (f"{index}. {trip['destination']} — {trip['duration_days']} дн."),
                f"Сохранён: {created_at}",
                "",
            ]
        )

    lines.append("Выберите действие кнопкой под сообщением.")

    return "\n".join(lines)


def format_created_at(
    created_at: str,
) -> str:
    """
    Преобразует ISO-время backend в короткую дату.
    """

    try:
        parsed_datetime = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        return parsed_datetime.strftime("%d.%m.%Y")

    except ValueError:
        return "дата неизвестна"
