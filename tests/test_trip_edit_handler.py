"""Тесты Telegram-обработчика изменения маршрута."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

import app.bot.handlers.history as history_handler
from app.bot.api_client import BackendError


def build_message() -> SimpleNamespace:
    """Создаёт сообщение с отдельным служебным ответом."""

    progress_message = SimpleNamespace(delete=AsyncMock())
    return SimpleNamespace(
        text="Сделай вечер спокойнее",
        from_user=SimpleNamespace(id=9000000001),
        answer=AsyncMock(return_value=progress_message),
        progress_message=progress_message,
    )


def build_state() -> SimpleNamespace:
    """Возвращает FSM с выбранным маршрутом."""

    return SimpleNamespace(
        get_data=AsyncMock(return_value={"editing_trip_id": 7}),
        clear=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_edit_instruction_sends_updated_trip_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Успешное изменение возвращает новую полную версию."""

    message = build_message()
    state = build_state()
    captured_arguments: dict[str, Any] = {}

    async def fake_edit_trip(**arguments: Any) -> dict[str, Any]:
        captured_arguments.update(arguments)
        return {"trip_id": 7, "updated": True}

    monkeypatch.setattr(history_handler, "edit_trip", fake_edit_trip)
    monkeypatch.setattr(history_handler, "format_trip_plan", lambda _: "Маршрут")
    monkeypatch.setattr(
        history_handler,
        "split_text",
        lambda _: ["Обновлённая часть 1", "Обновлённая часть 2"],
    )

    await history_handler.trip_edit_instruction_handler(
        message=message,
        state=state,
    )

    assert captured_arguments == {
        "telegram_id": 9000000001,
        "trip_id": 7,
        "instruction": "Сделай вечер спокойнее",
    }
    message.answer.assert_has_awaits(
        [
            call("✏️ Проверяю и обновляю маршрут..."),
            call("Обновлённая часть 1", reply_markup=None),
            call(
                "Обновлённая часть 2",
                reply_markup=history_handler.build_trip_actions_keyboard(7),
            ),
        ]
    )
    state.clear.assert_awaited_once_with()
    message.progress_message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_edit_error_keeps_selected_trip_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После безопасной backend-ошибки пользователь может уточнить фразу."""

    message = build_message()
    state = build_state()

    async def fake_edit_trip(**_: Any) -> dict[str, Any]:
        raise BackendError("Не удалось найти музей.")

    monkeypatch.setattr(history_handler, "edit_trip", fake_edit_trip)

    await history_handler.trip_edit_instruction_handler(
        message=message,
        state=state,
    )

    state.clear.assert_not_awaited()
    message.answer.assert_awaited_with(
        "Не удалось изменить маршрут:\nНе удалось найти музей.\n\n"
        "Отправьте исправленный вариант или нажмите «Отмена»."
    )
    message.progress_message.delete.assert_awaited_once_with()
