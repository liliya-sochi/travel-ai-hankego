"""
Обработчик истории сохранённых маршрутов.
"""

from datetime import datetime
from typing import Any

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.api_client import (
    BackendError,
    get_trip_history,
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

    for index, trip in enumerate(trips, start=1):
        created_at = format_created_at(
            str(trip["created_at"])
        )

        lines.extend(
            [
                (
                    f"{index}. {trip['destination']} — "
                    f"{trip['duration_days']} дн."
                ),
                (
                    f"Сохранён: {created_at} · "
                    f"ID: {trip['trip_id']}"
                ),
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

        return parsed_datetime.strftime("%d.%m.%Y")

    except ValueError:
        # Некорректная дата не должна ломать весь ответ.
        return "дата неизвестна"