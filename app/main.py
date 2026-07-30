"""
Главная точка входа FastAPI-приложения.

Здесь создаётся приложение, подключаются router-файлы
и настраивается инфраструктура HTTP-приложения.
"""

import logging
from contextlib import asynccontextmanager
from time import perf_counter
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from app.api.trip import router as trip_router
from app.api.user import router as user_router
from app.config import get_settings
from app.core.logging import configure_logging


settings = get_settings()
configure_logging(settings.log_level)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """
    Выполняет действия при запуске и остановке FastAPI.
    """

    logger.info("HankeGo API started")

    yield

    logger.info("HankeGo API stopped")


app = FastAPI(
    title="HankeGo API",
    description="Backend AI-помощника для планирования путешествий.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_http_request(
    request: Request,
    call_next,
) -> Response:
    """
    Записывает результат и длительность каждого HTTP-запроса.

    Тело запроса и query-параметры намеренно не логируются,
    чтобы не сохранять пользовательские данные.
    """

    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    started_at = perf_counter()

    try:
        response = await call_next(request)

    except Exception:
        duration_ms = (perf_counter() - started_at) * 1000

        logger.exception(
            "HTTP request failed | request_id=%s | method=%s | "
            "path=%s | duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )

        raise

    duration_ms = (perf_counter() - started_at) * 1000

    logger.info(
        "HTTP request completed | request_id=%s | method=%s | "
        "path=%s | status=%s | duration_ms=%.2f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )

    response.headers["X-Request-ID"] = request_id

    return response


@app.get(
    "/info",
    tags=["System"],
    summary="Проверить работу backend",
)
async def get_info() -> dict[str, str]:
    """
    Возвращает техническое состояние backend.
    """

    return {
        "name": "HankeGo API",
        "status": "ok",
        "version": "0.1.0",
    }


app.include_router(
    user_router,
    prefix=settings.api_prefix,
    tags=["Users"],
)


app.include_router(
    trip_router,
    prefix=settings.api_prefix,
    tags=["Trips"],
)