"""
Контекст сквозного идентификатора запроса HankeGo.

Correlation ID связывает одно Telegram-событие,
HTTP-запрос к FastAPI и все внутренние логи этого запроса.
"""

from contextvars import ContextVar, Token
from uuid import UUID, uuid4


CORRELATION_ID_HEADER = "X-Request-ID"
EMPTY_CORRELATION_ID = "-"


_correlation_id: ContextVar[str | None] = ContextVar(
    "correlation_id",
    default=None,
)


def create_correlation_id() -> str:
    """Создаёт новый случайный UUID для одной цепочки вызовов."""

    return str(uuid4())


def resolve_correlation_id(header_value: str | None) -> str:
    """
    Принимает безопасный UUID из HTTP-заголовка или создаёт новый.

    Внешнее значение сначала проверяется, чтобы произвольный текст
    и управляющие символы не могли попасть в журналы приложения.
    """

    if header_value is None:
        return create_correlation_id()

    try:
        return str(UUID(header_value.strip()))

    except ValueError:
        return create_correlation_id()


def get_correlation_id() -> str | None:
    """Возвращает ID текущей async-задачи, если он установлен."""

    return _correlation_id.get()


def set_correlation_id(
    correlation_id: str,
) -> Token[str | None]:
    """Устанавливает ID и возвращает token для последующего сброса."""

    return _correlation_id.set(correlation_id)


def reset_correlation_id(
    token: Token[str | None],
) -> None:
    """Возвращает контекст в состояние до установки ID."""

    _correlation_id.reset(token)