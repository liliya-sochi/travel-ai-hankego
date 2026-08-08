"""
Единая настройка логирования HankeGo.

Все части приложения используют стандартный logging.
Настройка выполняется один раз в точке входа API или бота.
"""

import logging
from logging.config import dictConfig

from app.core.request_context import (
    EMPTY_CORRELATION_ID,
    get_correlation_id,
)

LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "correlation_id=%(correlation_id)s | %(message)s"
)


class CorrelationIdFilter(logging.Filter):
    """Добавляет correlation ID текущей операции в каждую запись."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Обогащает LogRecord и разрешает его дальнейшую обработку."""

        record.correlation_id = get_correlation_id() or EMPTY_CORRELATION_ID

        return True


def configure_logging(log_level: str = "INFO") -> None:
    """
    Настраивает единый формат логов приложения.

    log_level приходит из настроек приложения:
    DEBUG, INFO, WARNING, ERROR или CRITICAL.
    """

    normalized_level = log_level.upper()

    if normalized_level not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        normalized_level = "INFO"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "correlation_id": {
                    "()": CorrelationIdFilter,
                },
            },
            "formatters": {
                "default": {
                    "format": LOG_FORMAT,
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["correlation_id"],
                    "level": normalized_level,
                },
            },
            "root": {
                "handlers": ["console"],
                "level": normalized_level,
            },
            "loggers": {
                "uvicorn": {
                    "handlers": ["console"],
                    "level": normalized_level,
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["console"],
                    "level": normalized_level,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["console"],
                    "level": normalized_level,
                    "propagate": False,
                },
                "aiogram": {
                    "handlers": ["console"],
                    "level": normalized_level,
                    "propagate": False,
                },
                "httpx": {
                    "handlers": ["console"],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
        }
    )
