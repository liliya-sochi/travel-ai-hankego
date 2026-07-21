"""
Запуск Telegram-бота HankeGo.

Доступные команды:

/start
    Показывает краткую инструкцию.

/plan <описание поездки>
    Создаёт маршрут через FastAPI и Groq.
"""

import asyncio
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.bot.api_client import BackendError, create_trip_plan
from app.config import get_settings


# Router хранит обработчики команд Telegram.
router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    """
    Обрабатывает команду /start.
    """

    await message.answer(
        "Привет! Я HankeGo — AI-помощник по путешествиям.\n\n"
        "Опиши желаемую поездку после команды /plan.\n\n"
        "Например:\n"
        "/plan Хочу на 5 дней в Стамбул. "
        "Люблю архитектуру, прогулки и местную еду."
    )


@router.message(Command("plan"))
async def plan_handler(
    message: Message,
    command: CommandObject,
) -> None:
    """
    Получает текст после команды /plan,
    отправляет его в FastAPI и показывает готовый маршрут.

    Например, в сообщении:

    /plan Хочу на неделю в Японию

    command.args будет содержать:

    Хочу на неделю в Японию
    """

    # command.args может быть None, если пользователь
    # отправил только /plan без описания.
    prompt = (command.args or "").strip()

    if len(prompt) < 10:
        await message.answer(
            "После /plan опиши поездку подробнее.\n\n"
            "Например:\n"
            "/plan Хочу на неделю в Японию. "
            "Люблю природу и современную архитектуру."
        )
        return

    # Сообщаем пользователю, что запрос принят.
    # Вызов Groq может занять несколько секунд.
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

    # Telegram ограничивает размер одного текстового сообщения
    # 4096 символами. Поэтому длинный маршрут разделяется
    # на несколько сообщений.
    for text_part in split_text(formatted_plan):
        await message.answer(text_part)


def format_trip_plan(trip_plan: dict[str, Any]) -> str:
    """
    Превращает Python-словарь от backend
    в читаемый текст для Telegram.

    Backend возвращает структурированные данные,
    а эта функция отвечает только за их отображение.
    """

    destination = trip_plan["destination"]
    duration_days = trip_plan["duration_days"]
    summary = trip_plan["summary"]
    days = trip_plan["days"]
    practical_tips = trip_plan.get("practical_tips", [])

    # Части сообщения сначала складываются в список.
    # В конце они объединяются символом новой строки.
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

        # Пустая строка отделяет один день от другого.
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
    Разделяет длинный текст на несколько Telegram-сообщений.

    У Telegram предел одного сообщения — 4096 символов.
    Используем 3800, чтобы оставить безопасный запас.
    """

    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    current_part: list[str] = []
    current_length = 0

    # Разделяем текст по строкам, чтобы по возможности
    # не разрывать предложение посередине.
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


async def run_bot() -> None:
    """
    Создаёт Telegram-бота и запускает получение сообщений.
    """

    settings = get_settings()

    bot = Bot(
        token=settings.telegram_bot_token,
    )

    # Dispatcher управляет получением событий от Telegram
    # и передаёт их подходящим обработчикам Router.
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    # Удаляем старый webhook, если он когда-либо был настроен.
    # Для локального запуска используем polling.
    await bot.delete_webhook(
        drop_pending_updates=True,
    )

    try:
        # start_polling работает постоянно и получает
        # новые сообщения от Telegram.
        await dispatcher.start_polling(bot)
    finally:
        # Корректно закрываем сетевое соединение,
        # когда бот останавливается.
        await bot.session.close()


if __name__ == "__main__":
    # asyncio.run запускает асинхронную функцию run_bot.
    asyncio.run(run_bot())