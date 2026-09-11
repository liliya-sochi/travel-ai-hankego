"""Тесты справки Telegram-бота."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.start import HELP_MESSAGE, help_handler
from app.bot.keyboards import build_main_menu_keyboard


@pytest.mark.asyncio
async def test_help_restores_main_menu() -> None:
    """Команда показывает быстрые действия и завершает старый диалог."""

    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())

    await help_handler(
        message=message,
        state=state,
    )

    state.clear.assert_awaited_once_with()
    message.answer.assert_awaited_once_with(
        HELP_MESSAGE,
        reply_markup=build_main_menu_keyboard(),
    )
