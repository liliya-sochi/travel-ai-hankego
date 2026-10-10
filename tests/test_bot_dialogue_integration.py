"""Маршрутизация Telegram updates и жизненный цикл FSM в настоящем Redis."""

import os
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from itertools import count
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Message, Update, User

import app.bot.api_client as api_client
from app.bot.handlers.plan import PLAN_START_MESSAGE
from app.bot.keyboards import (
    CANCEL_BUTTON_TEXT,
    MY_TRIPS_BUTTON_TEXT,
    NEW_TRIP_BUTTON_TEXT,
)
from app.bot.main import create_dispatcher
from app.bot.services.trip_formatter import format_trip_plan
from app.bot.states import TripEditing, TripPlanning
from app.core.request_context import get_correlation_id
from app.schemas.trip import TripDraft

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

TEST_USER_ID = 9000000001
TEST_BOT_TOKEN = "123456:TEST_TOKEN_FOR_LOCAL_UPDATES_ONLY"
UPDATE_IDS = count(1)


def get_test_redis_url() -> str:
    """Разрешает только явно заданный локальный Redis database 15."""

    redis_url = os.getenv("TEST_REDIS_URL")
    if redis_url is None:
        pytest.skip("TEST_REDIS_URL не задан: Telegram/Redis тесты пропущены.")
    parsed = urlparse(redis_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/15":
        raise RuntimeError("Telegram-тесты разрешены только для локального Redis /15.")
    return redis_url


class RecordingTelegramSession(BaseSession):
    """Перехватывает отправку сообщений; запросов к Telegram нет."""

    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        """У поддельного транспорта нет сетевых ресурсов."""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — контракт BaseSession.
    ) -> Any:
        """Возвращает типизированный ответ только для ожидаемых методов."""

        self.methods.append(method)
        if isinstance(method, GetMe):
            return User(
                id=bot.id, is_bot=True, first_name="Test", username="hankego_test_bot"
            )
        if isinstance(method, SendMessage):
            return Message.model_validate(
                {
                    "message_id": len(self.methods),
                    "date": datetime.now(UTC),
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
        if isinstance(method, (DeleteMessage, AnswerCallbackQuery)):
            return True
        raise AssertionError(f"Неожиданный Telegram-метод: {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — контракт BaseSession.
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        """Загрузка файлов не входит в проверяемые сценарии."""

        raise AssertionError("Тест не должен загружать файлы Telegram.")
        yield b""  # Делает метод асинхронным генератором по контракту BaseSession.

    @property
    def replies(self) -> list[SendMessage]:
        """Возвращает отправленные ответы без служебных Telegram-методов."""

        return [method for method in self.methods if isinstance(method, SendMessage)]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def dispatcher() -> AsyncIterator[Dispatcher]:
    """Подключает production routers один раз и очищает только свой префикс."""

    prefix = f"test:bot-dialogue:{uuid4().hex}"
    storage = RedisStorage.from_url(
        get_test_redis_url(),
        key_builder=DefaultKeyBuilder(prefix=prefix, with_bot_id=True),
    )
    await storage.redis.ping()
    dispatcher = create_dispatcher(storage)
    try:
        yield dispatcher
    finally:
        keys = [key async for key in storage.redis.scan_iter(match=f"{prefix}:*")]
        if keys:
            await storage.redis.delete(*keys)
        await dispatcher.fsm.close()


@pytest_asyncio.fixture(loop_scope="module")
async def bot(dispatcher: Dispatcher) -> AsyncIterator[Bot]:
    """Каждый тест получает новый транспорт и пустое состояние пользователя."""

    bot = Bot(TEST_BOT_TOKEN, session=RecordingTelegramSession())
    state = get_state(dispatcher, bot)
    await state.clear()
    try:
        yield bot
    finally:
        await state.clear()
        await bot.session.close()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Подменяет HTTP-границу; handlers, клиент и форматтер остаются настоящими."""

    backend = AsyncMock()
    monkeypatch.setattr(api_client, "_request_backend", backend)
    return backend


def get_state(
    dispatcher: Dispatcher, bot: Bot, user_id: int = TEST_USER_ID
) -> FSMContext:
    """Использует тот же ключ FSM, который выбирает Dispatcher."""

    return dispatcher.fsm.get_context(bot, chat_id=user_id, user_id=user_id)


async def send_update(
    dispatcher: Dispatcher,
    bot: Bot,
    *,
    text: str | None = None,
    callback: str | None = None,
    user_id: int = TEST_USER_ID,
) -> None:
    """Подаёт Update в настоящий Dispatcher, включая фильтры и middleware."""

    update_id = next(UPDATE_IDS)
    user = {"id": user_id, "is_bot": False, "first_name": "Test"}
    message: dict[str, Any] = {
        "message_id": update_id,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "private"},
        "from": user,
    }
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
    elif callback is None:
        message["photo"] = [
            {"file_id": "test", "file_unique_id": "test", "width": 1, "height": 1}
        ]
    payload: dict[str, Any] = {"update_id": update_id}
    if callback is None:
        payload["message"] = message
    else:
        payload["callback_query"] = {
            "id": str(update_id),
            "from": user,
            "chat_instance": "test",
            "data": callback,
            "message": message,
        }
    await dispatcher.feed_update(bot, Update.model_validate(payload))
    assert get_correlation_id() is None


def intake(draft: TripDraft) -> dict[str, Any]:
    """Фиксирует ответ backend; распознавание параметров здесь не проверяется."""

    missing = []
    if draft.destination is None:
        missing.append("destination")
    if draft.duration_days is None:
        missing.append("duration_days")
    question = (
        "Куда хотите поехать?"
        if draft.destination is None
        else "На сколько дней планируете поездку?"
    )
    return {
        "intent": "plan_trip",
        "draft": draft.model_dump(mode="json"),
        "missing_required_fields": missing,
        "ready_to_generate": not missing,
        "next_question": question if missing else None,
    }


def trip() -> dict[str, Any]:
    """Возвращает фиксированный маршрут HTTP backend без генерации моделью."""

    return {
        "trip_id": 7,
        "destination": "Токио",
        "duration_days": 1,
        "summary": "Музей и парки",
        "editable": True,
        "days": [
            {
                "day": 1,
                "title": "Музеи и парки",
                "morning": ["Mitsubishi Ichigokan Museum: посетить музей."],
                "afternoon": ["Парк Хибия: прогуляться по парку."],
                "evening": ["Спокойный отдых"],
            }
        ],
        "practical_tips": [],
    }


async def test_dialogue_survives_new_redis_connection_and_opens_history(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
) -> None:
    """Сохраняет параметры, генерирует после уточнения и открывает тот же план."""

    parks = TripDraft(interests="Парки")
    partial = TripDraft(
        destination="Токио",
        interests="Парки",
        must_visit_places=["Mitsubishi Ichigokan Museum"],
    )
    complete = partial.model_copy(update={"duration_days": 1})
    backend.side_effect = [
        {},
        intake(parks),
        intake(partial),
        intake(complete),
        trip(),
        {
            "trips": [
                {
                    "trip_id": 7,
                    "destination": "Токио",
                    "duration_days": 1,
                    "created_at": "2026-10-10T04:00:00Z",
                }
            ]
        },
        trip(),
    ]
    await send_update(dispatcher, bot, text="/start")
    assert await get_state(dispatcher, bot).get_data() == {}
    await send_update(
        dispatcher, bot, text="Хочу спланировать поездку. Интересуют парки."
    )
    assert bot.session.replies[-1].text == "Куда хотите поехать?"
    await send_update(
        dispatcher,
        bot,
        text="Токио. Обязательно хочу увидеть Mitsubishi Ichigokan Museum.",
    )
    assert backend.await_args_list[2].kwargs["payload"]["draft"] == parks.model_dump(
        mode="json"
    )
    state = get_state(dispatcher, bot)
    assert await state.get_state() == TripPlanning.collecting.state
    assert (await state.get_data())["draft"] == partial.model_dump(mode="json")
    assert backend.await_count == 3
    storage = dispatcher.storage
    assert isinstance(storage, RedisStorage)
    replacement = RedisStorage.from_url(
        get_test_redis_url(), key_builder=storage.key_builder
    )
    # Новый storage и соединение получают параметры из Redis, а не из памяти.
    dispatcher.fsm.storage = replacement
    await storage.close()
    try:
        await send_update(dispatcher, bot, text="На один день.")
        assert backend.await_args_list[3].kwargs["payload"][
            "draft"
        ] == partial.model_dump(mode="json")
        assert backend.await_args_list[4].kwargs["payload"][
            "preferences"
        ] == complete.model_dump(mode="json")
        assert await get_state(dispatcher, bot).get_state() is None
        assert await get_state(dispatcher, bot).get_data() == {}
        assert bot.session.replies[-1].text == format_trip_plan(trip())
        assert (
            bot.session.replies[-1].reply_markup.inline_keyboard[0][0].callback_data
            == "trip_edit_request:7"
        )
        await send_update(dispatcher, bot, text=MY_TRIPS_BUTTON_TEXT)
        await send_update(dispatcher, bot, callback="trip_open:7")
        assert [call.kwargs["path"] for call in backend.await_args_list] == [
            "/users/telegram",
            "/trip-intake",
            "/trip-intake",
            "/trip-intake",
            "/trip-plan",
            "/trip-history",
            "/trip-details",
        ]
        assert bot.session.replies[-1].text == format_trip_plan(trip())
        assert (
            sum(isinstance(method, DeleteMessage) for method in bot.session.methods)
            == 1
        )
    finally:
        dispatcher.fsm.storage = storage
        await replacement.close()


async def test_generation_failure_preserves_draft_for_short_retry(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
) -> None:
    """После ошибки генерации новая реплика получает весь сохранённый черновик."""

    draft = TripDraft(
        destination="Токио",
        duration_days=1,
        interests="Парки",
        must_visit_places=["Mitsubishi Ichigokan Museum"],
    )
    backend.side_effect = [
        intake(draft),
        api_client.BackendError("AI-сервис временно недоступен."),
        intake(draft),
        trip(),
    ]
    await send_update(
        dispatcher, bot, text="Один день в Токио: парки и Mitsubishi Ichigokan Museum."
    )
    state = get_state(dispatcher, bot)
    assert await state.get_state() == TripPlanning.collecting.state
    assert (await state.get_data())["draft"] == draft.model_dump(mode="json")
    assert "Черновик сохранён" in bot.session.replies[-1].text
    await send_update(dispatcher, bot, text="Попробуй ещё раз.")
    assert backend.await_args_list[2].kwargs["payload"]["draft"] == draft.model_dump(
        mode="json"
    )
    assert backend.await_args_list[3].kwargs["payload"][
        "preferences"
    ] == draft.model_dump(mode="json")
    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert sum(isinstance(method, DeleteMessage) for method in bot.session.methods) == 2


async def test_bad_intake_response_preserves_previous_draft(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
) -> None:
    """Неверный HTTP-контракт не повреждает ранее сохранённые параметры."""

    draft = TripDraft(destination="Токио", interests="Парки")
    backend.side_effect = [intake(draft), {"unexpected": True}]
    await send_update(dispatcher, bot, text="Хочу в Токио, интересуют парки.")
    await send_update(dispatcher, bot, text="На один день.")
    state = get_state(dispatcher, bot)
    assert await state.get_state() == TripPlanning.collecting.state
    assert (await state.get_data())["draft"] == draft.model_dump(mode="json")
    assert "Не удалось разобрать сообщение" in bot.session.replies[-1].text
    assert backend.await_count == 2


@pytest.mark.parametrize(
    "command",
    ["/plan Хочу один день в Токио", "/plan@hankego_test_bot Хочу один день в Токио"],
)
async def test_plan_command_keeps_arguments(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
    command: str,
) -> None:
    """Команда с параметрами должна попасть в intake, а не только начать FSM."""

    backend.side_effect = [
        intake(TripDraft(destination="Токио", duration_days=1)),
        trip(),
    ]
    await send_update(dispatcher, bot, text=command)
    assert backend.await_count == 2
    assert (
        backend.await_args_list[0].kwargs["payload"]["user_message"]
        == "Хочу один день в Токио"
    )
    assert backend.await_args_list[0].kwargs["payload"][
        "draft"
    ] == TripDraft().model_dump(mode="json")
    assert await get_state(dispatcher, bot).get_state() is None


@pytest.mark.parametrize(
    "action", ["/plan", NEW_TRIP_BUTTON_TEXT, CANCEL_BUTTON_TEXT, MY_TRIPS_BUTTON_TEXT]
)
async def test_menu_actions_leave_editing_without_intake(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
    action: str,
) -> None:
    """Служебные кнопки не перехватываются редактированием или свободным текстом."""

    await send_update(dispatcher, bot, callback="trip_edit_request:7")
    state = get_state(dispatcher, bot)
    assert await state.get_state() == TripEditing.waiting_instruction.state
    backend.return_value = {"trips": []}
    await send_update(dispatcher, bot, text=action)
    if action in {"/plan", NEW_TRIP_BUTTON_TEXT}:
        assert await state.get_state() == TripPlanning.collecting.state
        assert await state.get_data() == {"draft": TripDraft().model_dump(mode="json")}
        assert bot.session.replies[-1].text == PLAN_START_MESSAGE
    else:
        assert await state.get_state() is None
        assert await state.get_data() == {}
    assert [call.kwargs["path"] for call in backend.await_args_list] == (
        ["/trip-history"] if action == MY_TRIPS_BUTTON_TEXT else []
    )


async def test_edit_instruction_is_routed_before_free_text_and_can_retry(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
) -> None:
    """Callback выбирает маршрут; ошибка сохраняет выбор до успешной правки."""

    backend.side_effect = [
        api_client.BackendError("Нельзя изменить количество дней."),
        trip(),
    ]
    await send_update(dispatcher, bot, callback="trip_edit_request:7")
    await send_update(dispatcher, bot, text="Сделай маршрут на два дня.")
    state = get_state(dispatcher, bot)
    assert await state.get_state() == TripEditing.waiting_instruction.state
    assert await state.get_data() == {"editing_trip_id": 7}
    await send_update(dispatcher, bot, text="Тогда сделай вечер спокойнее.")
    assert [call.kwargs["path"] for call in backend.await_args_list] == [
        "/trip-edit",
        "/trip-edit",
    ]
    assert backend.await_args_list[1].kwargs["payload"] == {
        "telegram_id": TEST_USER_ID,
        "trip_id": 7,
        "instruction": "Тогда сделай вечер спокойнее.",
    }
    assert await state.get_state() is None
    assert await state.get_data() == {}


async def test_users_have_separate_drafts_and_non_text_keeps_state(
    dispatcher: Dispatcher,
    bot: Bot,
    backend: AsyncMock,
) -> None:
    """Чужой диалог и фотография не заменяют текущий черновик."""

    tokyo = TripDraft(destination="Токио", interests="Парки")
    istanbul = TripDraft(destination="Стамбул", interests="История")
    other_user = TEST_USER_ID + 1
    backend.side_effect = [intake(tokyo), intake(istanbul), intake(tokyo)]
    await send_update(dispatcher, bot, text="Хочу в Токио, интересуют парки.")
    await send_update(
        dispatcher, bot, text="Хочу в Стамбул, интересует история.", user_id=other_user
    )
    await send_update(dispatcher, bot)
    assert "только из текста" in bot.session.replies[-1].text
    assert backend.await_count == 2
    await send_update(dispatcher, bot, text="Пока не решила, на сколько дней.")
    assert backend.await_args_list[2].kwargs["payload"]["draft"] == tokyo.model_dump(
        mode="json"
    )
    assert (await get_state(dispatcher, bot, other_user).get_data())[
        "draft"
    ] == istanbul.model_dump(mode="json")
