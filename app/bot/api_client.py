"""
Клиент Telegram-бота для общения с FastAPI backend.

Telegram-бот не обращается к LLM или PostgreSQL напрямую.
Все операции выполняются через внутренний HTTP API.
"""

from typing import Any, Literal

import httpx

from app.config import get_settings
from app.core.request_context import (
    CORRELATION_ID_HEADER,
    create_correlation_id,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from app.schemas.trip import TripPreferences


HttpMethod = Literal[
    "GET",
    "POST",
    "PUT",
    "DELETE",
]


class BackendError(Exception):
    """
    Безопасная ошибка обращения к FastAPI backend.
    """


async def _request_backend(
    *,
    method: HttpMethod,
    path: str,
    payload: dict[str, Any],
    timeout: float,
    timeout_message: str,
    default_error_message: str,
) -> dict[str, Any]:
    """
    Выполняет авторизованный запрос к внутреннему API.
    """

    settings = get_settings()

    correlation_id = get_correlation_id()
    context_token = None

    # При обычной работе ID уже установило Telegram middleware.
    # Резервное создание нужно для прямых вызовов клиента и тестов.
    if correlation_id is None:
        correlation_id = create_correlation_id()
        context_token = set_correlation_id(
            correlation_id
        )

    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}"
        f"{path}"
    )

    headers = {
        "X-Internal-API-Key": (
            settings.internal_api_key.get_secret_value()
        ),
        CORRELATION_ID_HEADER: correlation_id,
    }

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
        ) as client:
            response = await client.request(
                method=method,
                url=url,
                json=payload,
                headers=headers,
            )

        response.raise_for_status()

        response_data = response.json()

        if not isinstance(response_data, dict):
            raise ValueError(
                "Backend response must be a JSON object."
            )

        return response_data

    except httpx.TimeoutException as error:
        raise BackendError(
            timeout_message
        ) from error

    except httpx.HTTPStatusError as error:
        error_message = default_error_message

        try:
            error_data = error.response.json()

            if isinstance(error_data, dict):
                detail = error_data.get("detail")

                if isinstance(detail, str):
                    error_message = detail

        except ValueError:
            pass

        raise BackendError(
            error_message
        ) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API."
        ) from error

    except ValueError as error:
        raise BackendError(
            "Backend вернул данные в неправильном формате."
        ) from error

    finally:
        if context_token is not None:
            reset_correlation_id(context_token)


async def register_telegram_user(
    telegram_id: int,
    first_name: str,
) -> None:
    """
    Создаёт или обновляет Telegram-пользователя.
    """

    await _request_backend(
        method="PUT",
        path="/users/telegram",
        payload={
            "telegram_id": telegram_id,
            "first_name": first_name,
        },
        timeout=10.0,
        timeout_message=(
            "Backend не успел зарегистрировать пользователя."
        ),
        default_error_message=(
            "Backend не смог зарегистрировать пользователя."
        ),
    )


async def create_trip_plan(
    *,
    telegram_id: int,
    first_name: str,
    preferences: TripPreferences,
) -> dict[str, Any]:
    """
    Создаёт и сохраняет маршрут.
    """

    return await _request_backend(
        method="POST",
        path="/trip-plan",
        payload={
            "telegram_id": telegram_id,
            "first_name": first_name,
            "preferences": preferences.model_dump(
                mode="json"
            ),
        },
        timeout=150.0,
        timeout_message=(
            "Backend не успел подготовить маршрут."
        ),
        default_error_message=(
            "Backend не смог создать маршрут."
        ),
    )


async def get_trip_history(
    *,
    telegram_id: int,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Получает историю сохранённых маршрутов.
    """

    return await _request_backend(
        method="POST",
        path="/trip-history",
        payload={
            "telegram_id": telegram_id,
            "limit": limit,
        },
        timeout=10.0,
        timeout_message=(
            "Backend не успел загрузить маршруты."
        ),
        default_error_message=(
            "Backend не смог загрузить маршруты."
        ),
    )


async def get_trip_details(
    *,
    telegram_id: int,
    trip_id: int,
) -> dict[str, Any]:
    """
    Получает полный сохранённый маршрут.
    """

    return await _request_backend(
        method="POST",
        path="/trip-details",
        payload={
            "telegram_id": telegram_id,
            "trip_id": trip_id,
        },
        timeout=10.0,
        timeout_message=(
            "Backend не успел загрузить маршрут."
        ),
        default_error_message=(
            "Backend не смог загрузить маршрут."
        ),
    )


async def delete_trip(
    *,
    telegram_id: int,
    trip_id: int,
) -> dict[str, Any]:
    """
    Удаляет сохранённый маршрут.
    """

    return await _request_backend(
        method="POST",
        path="/trip-delete",
        payload={
            "telegram_id": telegram_id,
            "trip_id": trip_id,
        },
        timeout=10.0,
        timeout_message=(
            "Backend не успел удалить маршрут."
        ),
        default_error_message=(
            "Backend не смог удалить маршрут."
        ),
    )