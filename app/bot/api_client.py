"""
Клиент для общения Telegram-бота с FastAPI backend.

Telegram-бот не обращается к Groq напрямую.
Он отправляет запрос нашему backend, а backend уже:
1. обращается к языковой модели;
2. проверяет ответ;
3. возвращает структурированный план.
"""

from typing import Any

import httpx

from app.config import get_settings


class BackendError(Exception):
    """
    Ошибка при обращении Telegram-бота к backend.

    Благодаря собственному исключению main.py не должен знать
    детали работы библиотеки httpx.
    """


async def register_telegram_user(
    telegram_id: int,
    first_name: str,
) -> None:
    """
    Создаёт или обновляет Telegram-пользователя через FastAPI.
    """

    settings = get_settings()

    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}/users/telegram"
    )

    try:
        # Регистрация пользователя должна выполняться быстро,
        # поэтому здесь используется короткий тайм-аут.
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.put(
                url=url,
                json={
                    "telegram_id": telegram_id,
                    "first_name": first_name,
                },
            )

        response.raise_for_status()

    except httpx.TimeoutException as error:
        raise BackendError(
            "Backend не успел зарегистрировать пользователя."
        ) from error

    except httpx.HTTPStatusError as error:
        try:
            error_data = error.response.json()
            error_message = error_data.get(
                "detail",
                "Backend не смог зарегистрировать пользователя.",
            )
        except ValueError:
            error_message = (
                "Backend вернул неправильный ответ "
                "при регистрации пользователя."
            )

        raise BackendError(str(error_message)) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API."
        ) from error


async def create_trip_plan(
    *,
    telegram_id: int,
    first_name: str,
    prompt: str,
) -> dict[str, Any]:
    """
    Создаёт и сохраняет маршрут через FastAPI.
    """

    settings = get_settings()

    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}/trip-plan"
    )

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                url=url,
                json={
                    "telegram_id": telegram_id,
                    "first_name": first_name,
                    "prompt": prompt,
                },
            )

        response.raise_for_status()

        return response.json()

    except httpx.TimeoutException as error:
        raise BackendError(
            "Backend не успел подготовить маршрут."
        ) from error

    except httpx.HTTPStatusError as error:
        try:
            error_data = error.response.json()
            error_message = error_data.get(
                "detail",
                "Backend вернул ошибку.",
            )
        except ValueError:
            error_message = (
                "Backend вернул неправильный ответ."
            )

        raise BackendError(str(error_message)) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API. "
            "Проверь, запущен ли FastAPI."
        ) from error

    except ValueError as error:
        raise BackendError(
            "Backend вернул данные в неправильном формате."
        ) from error


async def get_trip_history(
    *,
    telegram_id: int,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Получает историю маршрутов через FastAPI.
    """

    settings = get_settings()

    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}/trip-history"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url=url,
                json={
                    "telegram_id": telegram_id,
                    "limit": limit,
                },
            )

        response.raise_for_status()

        return response.json()

    except httpx.TimeoutException as error:
        raise BackendError(
            "Backend не успел загрузить маршруты."
        ) from error

    except httpx.HTTPStatusError as error:
        try:
            error_data = error.response.json()
            error_message = error_data.get(
                "detail",
                "Backend не смог загрузить маршруты.",
            )
        except ValueError:
            error_message = (
                "Backend вернул неправильный ответ."
            )

        raise BackendError(str(error_message)) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API."
        ) from error

    except ValueError as error:
        raise BackendError(
            "Backend вернул данные в неправильном формате."
        ) from error


async def get_trip_details(
    *,
    telegram_id: int,
    trip_id: int,
) -> dict[str, Any]:
    """
    Получает полный сохранённый маршрут через FastAPI.
    """

    settings = get_settings()

    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}/trip-details"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url=url,
                json={
                    "telegram_id": telegram_id,
                    "trip_id": trip_id,
                },
            )

        response.raise_for_status()

        return response.json()

    except httpx.TimeoutException as error:
        raise BackendError(
            "Backend не успел загрузить маршрут."
        ) from error

    except httpx.HTTPStatusError as error:
        try:
            error_data = error.response.json()
            error_message = error_data.get(
                "detail",
                "Backend не смог загрузить маршрут.",
            )
        except ValueError:
            error_message = (
                "Backend вернул неправильный ответ."
            )

        raise BackendError(str(error_message)) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API."
        ) from error

    except ValueError as error:
        raise BackendError(
            "Backend вернул данные в неправильном формате."
        ) from error


async def delete_trip(
    *,
    telegram_id: int,
    trip_id: int,
) -> dict[str, Any]:
    """
    Удаляет сохранённый маршрут через FastAPI.
    """

    settings = get_settings()

    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}/trip-delete"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url=url,
                json={
                    "telegram_id": telegram_id,
                    "trip_id": trip_id,
                },
            )

        response.raise_for_status()

        return response.json()

    except httpx.TimeoutException as error:
        raise BackendError(
            "Backend не успел удалить маршрут."
        ) from error

    except httpx.HTTPStatusError as error:
        try:
            error_data = error.response.json()
            error_message = error_data.get(
                "detail",
                "Backend не смог удалить маршрут.",
            )
        except ValueError:
            error_message = (
                "Backend вернул неправильный ответ."
            )

        raise BackendError(str(error_message)) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API."
        ) from error

    except ValueError as error:
        raise BackendError(
            "Backend вернул данные в неправильном формате."
        ) from error