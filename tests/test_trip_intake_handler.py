"""
Тесты разговорного Telegram-обработчика поездки.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage

import app.bot.handlers.plan as plan_handler
from app.bot.api_client import BackendError
from app.bot.states import TripPlanning
from app.schemas.trip import (
    TripDraft,
    TripIntakeResponse,
    TripPreferences,
)


def build_message(
    text: str = "Хочу в Японию",
) -> SimpleNamespace:
    """
    Создаёт минимальный объект Telegram Message для unit-тестов.
    """

    progress_message = SimpleNamespace(
        delete=AsyncMock(),
    )

    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(
            id=9000000001,
            first_name="Liliya",
        ),
        answer=AsyncMock(
            return_value=progress_message,
        ),
        progress_message=progress_message,
    )


def build_state(
    data: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """
    Создаёт поддельный FSMContext без настоящего Redis.
    """

    stored_data = {} if data is None else data

    return SimpleNamespace(
        get_data=AsyncMock(
            return_value=stored_data,
        ),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        clear=AsyncMock(),
    )


def test_restore_trip_draft() -> None:
    """
    Проверяет восстановление типизированного черновика из Redis-словаря.
    """

    draft = plan_handler.restore_trip_draft(
        {
            "draft": {
                "destination": "Япония",
                "duration_days": 7,
                "travel_period": None,
                "budget": None,
                "interests": None,
            }
        }
    )

    assert draft == TripDraft(
        destination="Япония",
        duration_days=7,
    )


@pytest.mark.asyncio
async def test_incomplete_draft_is_saved_and_next_question_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Сохраняет неполный черновик и задаёт следующий вопрос.
    """

    message = build_message()
    current_draft = TripDraft()

    state = build_state(
        {
            "draft": current_draft.model_dump(mode="json"),
        }
    )

    intake_response = TripIntakeResponse(
        intent="plan_trip",
        draft=TripDraft(
            destination="Япония",
        ),
        missing_required_fields=["duration_days"],
        ready_to_generate=False,
        next_question="На сколько дней планируете поездку?",
    )

    captured_arguments: dict[str, Any] = {}

    async def fake_process_trip_intake(
        **arguments: Any,
    ) -> TripIntakeResponse:
        captured_arguments.update(arguments)
        return intake_response

    monkeypatch.setattr(
        plan_handler,
        "process_trip_intake",
        fake_process_trip_intake,
    )

    await plan_handler.handle_trip_message(
        message=message,
        state=state,
        user_message="Хочу в Японию",
    )

    assert captured_arguments == {
        "telegram_id": 9000000001,
        "user_message": "Хочу в Японию",
        "draft": current_draft,
    }

    state.set_state.assert_awaited_once_with(
        TripPlanning.collecting,
    )
    state.update_data.assert_awaited_once_with(
        draft=intake_response.draft.model_dump(mode="json"),
    )
    state.clear.assert_not_awaited()

    message.answer.assert_awaited_once_with("На сколько дней планируете поездку?")


@pytest.mark.asyncio
async def test_complete_draft_generates_and_sends_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Передаёт полный черновик в генерацию и очищает FSM после успеха.
    """

    message = build_message(
        "Хочу на неделю в Японию",
    )
    state = build_state()

    intake_response = TripIntakeResponse(
        intent="plan_trip",
        draft=TripDraft(
            destination="Япония",
            duration_days=7,
            travel_period=None,
            budget=None,
            interests="Современная архитектура",
        ),
        missing_required_fields=[],
        ready_to_generate=True,
        next_question=None,
    )

    async def fake_process_trip_intake(
        **_: Any,
    ) -> TripIntakeResponse:
        return intake_response

    captured_generation_arguments: dict[str, Any] = {}

    async def fake_create_trip_plan(
        **arguments: Any,
    ) -> dict[str, Any]:
        captured_generation_arguments.update(arguments)
        return {
            "status": "ok",
        }

    monkeypatch.setattr(
        plan_handler,
        "process_trip_intake",
        fake_process_trip_intake,
    )
    monkeypatch.setattr(
        plan_handler,
        "create_trip_plan",
        fake_create_trip_plan,
    )
    monkeypatch.setattr(
        plan_handler,
        "format_trip_plan",
        lambda _: "Готовый маршрут",
    )
    monkeypatch.setattr(
        plan_handler,
        "split_text",
        lambda _: [
            "Первая часть маршрута",
            "Вторая часть маршрута",
        ],
    )

    await plan_handler.handle_trip_message(
        message=message,
        state=state,
        user_message=message.text,
    )

    assert captured_generation_arguments == {
        "telegram_id": 9000000001,
        "first_name": "Liliya",
        "preferences": TripPreferences(
            destination="Япония",
            duration_days=7,
            interests="Современная архитектура",
        ),
    }

    message.answer.assert_has_awaits(
        [
            call("✈️ Генерирую и сохраняю маршрут..."),
            call("Первая часть маршрута"),
            call("Вторая часть маршрута"),
        ]
    )

    state.clear.assert_awaited_once_with()
    message.progress_message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_show_trips_intent_clears_draft_and_opens_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Переключает разговор с планирования на историю маршрутов.
    """

    message = build_message(
        "Покажи мои маршруты",
    )
    state = build_state(
        {
            "draft": TripDraft(
                destination="Япония",
            ).model_dump(mode="json"),
        }
    )

    async def fake_process_trip_intake(
        **_: Any,
    ) -> TripIntakeResponse:
        return TripIntakeResponse(
            intent="show_trips",
            draft=TripDraft(
                destination="Япония",
            ),
            missing_required_fields=["duration_days"],
            ready_to_generate=False,
            next_question=None,
        )

    fake_send_trip_history = AsyncMock()

    monkeypatch.setattr(
        plan_handler,
        "process_trip_intake",
        fake_process_trip_intake,
    )
    monkeypatch.setattr(
        plan_handler,
        "send_trip_history",
        fake_send_trip_history,
    )

    await plan_handler.handle_trip_message(
        message=message,
        state=state,
        user_message=message.text,
    )

    state.clear.assert_awaited_once_with()
    fake_send_trip_history.assert_awaited_once_with(message)
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_error_keeps_trip_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Не очищает Redis-черновик при временной ошибке генерации.
    """

    message = build_message()
    state = build_state()

    async def fake_create_trip_plan(
        **_: Any,
    ) -> dict[str, Any]:
        raise BackendError("Временная ошибка backend.")

    monkeypatch.setattr(
        plan_handler,
        "create_trip_plan",
        fake_create_trip_plan,
    )

    await plan_handler.generate_and_send_trip(
        message=message,
        state=state,
        preferences=TripPreferences(
            destination="Япония",
            duration_days=7,
        ),
    )

    message.answer.assert_has_awaits(
        [
            call("✈️ Генерирую и сохраняю маршрут..."),
            call(
                "Не удалось создать маршрут:\n"
                "Временная ошибка backend.\n\n"
                "Черновик сохранён — сообщение можно отправить ещё раз."
            ),
        ]
    )

    state.clear.assert_not_awaited()
    message.progress_message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_progress_message_deletion_error_is_non_fatal() -> None:
    """
    Ошибка удаления служебного сообщения не ломает основной сценарий.
    """

    progress_message = SimpleNamespace(
        delete=AsyncMock(
            side_effect=TelegramBadRequest(
                method=DeleteMessage(
                    chat_id=9000000001,
                    message_id=1,
                ),
                message="Message to delete not found",
            )
        ),
    )

    await plan_handler.delete_progress_message(
        progress_message,
    )

    progress_message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_start_new_trip_dialog_creates_empty_draft() -> None:
    """
    Кнопка новой поездки удаляет старый и создаёт пустой черновик.
    """

    message = build_message()
    state = build_state(
        {
            "draft": TripDraft(
                destination="Старое направление",
            ).model_dump(mode="json"),
        }
    )

    await plan_handler.start_new_trip_dialog(
        message=message,
        state=state,
    )

    state.clear.assert_awaited_once_with()
    state.set_state.assert_awaited_once_with(
        TripPlanning.collecting,
    )
    state.update_data.assert_awaited_once_with(
        draft=TripDraft().model_dump(mode="json"),
    )
    message.answer.assert_awaited_once_with(
        plan_handler.PLAN_START_MESSAGE,
    )
