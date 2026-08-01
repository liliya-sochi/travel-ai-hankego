"""
Обработчики сохранённых маршрутов.
"""

from datetime import datetime
from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.api_client import (
    BackendError,
    get_trip_details,
    get_trip_history,
)
from app.bot.services.trip_formatter import (
    format_trip_plan,
    split_text,
)


router = Router()


@router.message(Command("trips"))
async def trips_handler(
    message: Message,
) -> None:
    """
    Показывает последние маршруты пользователя.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await message.answer(
            "Не удалось определить пользователя Telegram."
        )
        return

    try:
        history = await get_trip_history(
            telegram_id=telegram_user.id,
            limit=10,
        )

    except BackendError as error:
        await message.answer(
            f"Не удалось загрузить маршруты:\n{error}"
        )
        return

    trips = history.get("trips", [])

    if not trips:
        await message.answer(
            "У вас пока нет сохранённых маршрутов.\n\n"
            "Создать первый маршрут: /plan"
        )
        return

    await message.answer(
        format_trip_history(trips)
    )


@router.message(Command("trip"))
async def trip_handler(
    message: Message,
) -> None:
    """
    Показывает полный сохранённый маршрут по ID.
    """

    telegram_user = message.from_user

    if telegram_user is None:
        await message.answer(
            "Не удалось определить пользователя Telegram."
        )
        return

    trip_id = extract_trip_id(
        message.text
    )

    if trip_id is None:
        await message.answer(
            "Укажите ID маршрута.\n\n"
            "Например: /trip 7\n\n"
            "Список маршрутов: /trips"
        )
        return

    try:
        trip = await get_trip_details(
            telegram_id=telegram_user.id,
            trip_id=trip_id,
        )

    except BackendError as error:
        await message.answer(
            f"Не удалось загрузить маршрут:\n{error}"
        )
        return

    formatted_trip = format_trip_plan(trip)

    for text_part in split_text(formatted_trip):
        await message.answer(text_part)


def extract_trip_id(
    message_text: str | None,
) -> int | None:
    """
    Извлекает положительный ID из команды /trip.
    """

    if message_text is None:
        return None

    command_parts = message_text.split(
        maxsplit=1
    )

    if len(command_parts) != 2:
        return None

    raw_trip_id = command_parts[1].strip()

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
        created_at = format_created_at(
            str(trip["created_at"])
        )

        trip_id = trip["trip_id"]

        lines.extend(
            [
                (
                    f"{index}. {trip['destination']} — "
                    f"{trip['duration_days']} дн."
                ),
                (
                    f"Сохранён: {created_at} · "
                    f"ID: {trip_id}"
                ),
                f"Открыть: /trip {trip_id}",
                "",
            ]
        )

    lines.append(
        "Создать новый маршрут: /plan"
    )

    return "\n".join(lines)


def format_created_at(
    created_at: str,
) -> str:
    """
    Преобразует ISO-время backend в короткую дату.
    """

    try:
        parsed_datetime = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        )

        return parsed_datetime.strftime(
            "%d.%m.%Y"
        )

    except ValueError:
        return "дата неизвестна"