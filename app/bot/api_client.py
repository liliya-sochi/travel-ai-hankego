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


async def create_trip_plan(prompt: str) -> dict[str, Any]:
    """
    Отправляет пользовательский запрос в FastAPI
    и возвращает готовый план поездки.

    Возвращаемое значение — обычный Python-словарь,
    созданный из JSON-ответа backend.
    """

    settings = get_settings()

    # Собираем полный адрес нашего endpoint:
    # http://127.0.0.1:8000/api/v1/trip-plan
    url = (
        f"{settings.backend_url.rstrip('/')}"
        f"{settings.api_prefix}/trip-plan"
    )

    try:
        # Backend обращается к LLM, поэтому ответ может занять
        # несколько секунд. Устанавливаем тайм-аут 90 секунд.
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                url=url,
                json={
                    "prompt": prompt,
                },
            )

        # Преобразует HTTP-статусы 400, 404, 500 и другие
        # в исключение HTTPStatusError.
        response.raise_for_status()

        # response.json() превращает JSON-ответ backend
        # в Python-словарь.
        return response.json()

    except httpx.TimeoutException as error:
        raise BackendError(
            "Backend не успел подготовить маршрут."
        ) from error

    except httpx.HTTPStatusError as error:
        # Пытаемся получить понятное описание ошибки,
        # которое FastAPI обычно хранит в поле detail.
        try:
            error_data = error.response.json()
            error_message = error_data.get(
                "detail",
                "Backend вернул ошибку.",
            )
        except ValueError:
            error_message = "Backend вернул неправильный ответ."

        raise BackendError(str(error_message)) from error

    except httpx.RequestError as error:
        raise BackendError(
            "Не удалось подключиться к HankeGo API. "
            "Проверь, запущен ли FastAPI."
        ) from error

    except ValueError as error:
        # Такое возможно, если backend вернул не JSON.
        raise BackendError(
            "Backend вернул данные в неправильном формате."
        ) from error