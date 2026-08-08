"""
Главная точка входа FastAPI-приложения.

Здесь создаётся приложение, подключаются router-файлы
и настраивается инфраструктура HTTP-приложения.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import (
    Depends,
    FastAPI,
    Request,
    Response,
)

from app.api.system import router as system_router
from app.api.trip import router as trip_router
from app.api.user import router as user_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis_client
from app.core.request_context import (
    CORRELATION_ID_HEADER,
    reset_correlation_id,
    resolve_correlation_id,
    set_correlation_id,
)
from app.core.security import verify_internal_api_key

settings = get_settings()
configure_logging(settings.log_level)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(
    application: FastAPI,
) -> AsyncIterator[None]:
    """
    Управляет ресурсами при запуске и остановке FastAPI.
    """

    redis_client = create_redis_client(settings.redis_url)

    application.state.redis_client = redis_client

    logger.info("HankeGo API started")

    try:
        yield

    finally:
        await redis_client.aclose()

        logger.info("HankeGo API stopped")


app = FastAPI(
    title="HankeGo API",
    description=("Backend AI-помощника для планирования путешествий."),
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_http_request(
    request: Request,
    call_next,
) -> Response:
    """
    Устанавливает correlation ID и записывает результат HTTP-запроса.

    Тело запроса и query-параметры намеренно не логируются,
    чтобы не сохранять пользовательские данные.
    """

    correlation_id = resolve_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
    context_token = set_correlation_id(correlation_id)
    started_at = perf_counter()

    try:
        response = await call_next(request)

    except Exception:
        duration_ms = (perf_counter() - started_at) * 1000

        logger.exception(
            "HTTP request failed | method=%s | path=%s | duration_ms=%.2f",
            request.method,
            request.url.path,
            duration_ms,
        )

        raise

    else:
        duration_ms = (perf_counter() - started_at) * 1000

        logger.info(
            "HTTP request completed | "
            "method=%s | path=%s | status=%s | "
            "duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )

        response.headers[CORRELATION_ID_HEADER] = correlation_id

        return response

    finally:
        reset_correlation_id(context_token)


# Системные endpoint остаются публичными,
# чтобы их мог вызывать мониторинг сервера.
app.include_router(
    system_router,
)


internal_api_dependencies = [
    Depends(verify_internal_api_key),
]


app.include_router(
    user_router,
    prefix=settings.api_prefix,
    tags=["Users"],
    dependencies=internal_api_dependencies,
)


app.include_router(
    trip_router,
    prefix=settings.api_prefix,
    tags=["Trips"],
    dependencies=internal_api_dependencies,
)
