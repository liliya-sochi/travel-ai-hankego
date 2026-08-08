"""
Middleware Telegram-бота для сквозного correlation ID.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.core.request_context import (
    create_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

TelegramHandler = Callable[
    [TelegramObject, dict[str, Any]],
    Awaitable[Any],
]


class CorrelationIdMiddleware(BaseMiddleware):
    """
    Создаёт отдельный correlation ID для каждого Telegram update.
    """

    async def __call__(
        self,
        handler: TelegramHandler,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Устанавливает ID на всё время обработки одного update."""

        context_token = set_correlation_id(create_correlation_id())

        try:
            return await handler(event, data)

        finally:
            reset_correlation_id(context_token)
