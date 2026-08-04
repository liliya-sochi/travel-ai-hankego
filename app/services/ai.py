"""
Сервис для обращения к языковой модели.

Этот файл отвечает только за работу с LLM:
- создаёт содержательные инструкции для модели;
- формирует строгую JSON Schema из Pydantic;
- отправляет HTTP-запрос;
- извлекает структурированный ответ;
- повторно проверяет его через Pydantic.
"""

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.schemas.trip import TripPlanResponse


logger = logging.getLogger(__name__)


REQUEST_TIMEOUT_SECONDS = 60.0
MAX_SEMANTIC_ATTEMPTS = 2
STRUCTURED_OUTPUT_NAME = "trip_plan"


SYSTEM_PROMPT = """
Ты — AI-помощник по планированию путешествий HankeGo.

Создай реалистичный и практичный план поездки
на основании запроса пользователя.

Содержательные правила:
- отвечай на русском языке;
- количество дней должно соответствовать запросу;
- количество элементов days должно быть равно duration_days;
- номера дней должны идти последовательно от 1;
- не выдумывай точные цены, расписания и часы работы;
- предупреждай, что цены и расписания нужно проверять отдельно;
- не добавляй чрезмерное количество мест на один день;
- для каждого дня заполняй morning, afternoon и evening;
- используй пустой список, когда активность действительно не нужна;
- practical_tips должен содержать полезные советы для поездки.
""".strip()


SEMANTIC_RETRY_PROMPT = """
Предыдущий план не прошёл проверку логической согласованности.

Создай весь план заново и обязательно проверь:
- количество элементов days равно duration_days;
- номера дней идут последовательно от 1 до duration_days.
""".strip()


class AIServiceError(Exception):
    """
    Безопасная ошибка сервиса языковой модели.

    Технические детали записываются в журнал приложения,
    но не передаются пользователю API.
    """


def _build_response_format() -> dict[str, Any]:
    """
    Создаёт строгий Structured Output из Pydantic-схемы.
    """

    return {
        "type": "json_schema",
        "json_schema": {
            "name": STRUCTURED_OUTPUT_NAME,
            "strict": True,
            "schema": (
                TripPlanResponse.model_json_schema()
            ),
        },
    }


def _build_request_payload(
    *,
    model: str,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Формирует тело запроса к OpenAI-совместимому API.
    """

    return {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "response_format": _build_response_format(),
    }


def _extract_model_text(
    response_data: dict[str, Any],
) -> str:
    """
    Извлекает Structured Output из ответа провайдера.
    """

    try:
        message = response_data[
            "choices"
        ][0]["message"]

    except (
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        raise AIServiceError(
            "AI-сервис вернул ответ неизвестной структуры."
        ) from error

    if not isinstance(message, dict):
        raise AIServiceError(
            "AI-сервис вернул ответ неизвестной структуры."
        )

    refusal = message.get("refusal")

    if (
        isinstance(refusal, str)
        and refusal.strip()
    ):
        logger.warning(
            "AI provider refused to generate a trip plan"
        )

        raise AIServiceError(
            "AI-сервис не смог обработать этот запрос."
        )

    model_text = message.get("content")

    if (
        not isinstance(model_text, str)
        or not model_text.strip()
    ):
        raise AIServiceError(
            "AI-сервис вернул пустой ответ."
        )

    return model_text


def _validate_trip_plan(
    model_text: str,
) -> TripPlanResponse:
    """
    Проверяет JSON и бизнес-правила через Pydantic.
    """

    return TripPlanResponse.model_validate_json(
        model_text
    )


async def _request_model(
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Выполняет один HTTP-запрос к AI-провайдеру.
    """

    try:
        response = await client.post(
            url=url,
            headers=headers,
            json=payload,
        )

        response.raise_for_status()

        response_data = response.json()

        if not isinstance(response_data, dict):
            raise ValueError(
                "AI provider response must be a JSON object."
            )

        return response_data

    except httpx.TimeoutException as error:
        logger.warning(
            "AI provider timeout after %.1f seconds",
            REQUEST_TIMEOUT_SECONDS,
        )

        raise AIServiceError(
            "AI-сервис временно не отвечает. "
            "Попробуйте ещё раз."
        ) from error

    except httpx.HTTPStatusError as error:
        provider_request_id = (
            error.response.headers.get(
                "x-request-id",
                "unknown",
            )
        )

        # Тело ответа не логируем:
        # оно может содержать пользовательские данные.
        logger.error(
            "AI provider HTTP error | "
            "status=%s | request_id=%s",
            error.response.status_code,
            provider_request_id,
        )

        raise AIServiceError(
            "AI-сервис временно недоступен. "
            "Попробуйте позже."
        ) from error

    except httpx.RequestError as error:
        logger.error(
            "AI provider connection error: %s",
            type(error).__name__,
        )

        raise AIServiceError(
            "Не удалось подключиться к AI-сервису."
        ) from error

    except (
        json.JSONDecodeError,
        ValueError,
    ) as error:
        logger.error(
            "AI provider returned invalid HTTP JSON."
        )

        raise AIServiceError(
            "AI-сервис вернул некорректный ответ."
        ) from error


async def generate_trip_plan(
    user_prompt: str,
) -> TripPlanResponse:
    """
    Создаёт проверенный план путешествия.

    Groq гарантирует соответствие JSON Schema.
    Pydantic дополнительно проверяет бизнес-правила,
    которые не выражаются обычной JSON Schema.
    """

    settings = get_settings()

    url = (
        f"{settings.llm_base_url.rstrip('/')}"
        "/chat/completions"
    )

    headers = {
        "Authorization": (
            f"Bearer {settings.llm_api_key}"
        ),
        "Content-Type": "application/json",
    }

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ]

    request_timeout = httpx.Timeout(
        timeout=REQUEST_TIMEOUT_SECONDS,
        connect=10.0,
    )

    async with httpx.AsyncClient(
        timeout=request_timeout,
    ) as client:
        for attempt in range(
            1,
            MAX_SEMANTIC_ATTEMPTS + 1,
        ):
            payload = _build_request_payload(
                model=settings.llm_model,
                messages=messages,
            )

            response_data = await _request_model(
                client=client,
                url=url,
                headers=headers,
                payload=payload,
            )

            model_text = _extract_model_text(
                response_data
            )

            try:
                return _validate_trip_plan(
                    model_text
                )

            except ValidationError as error:
                logger.warning(
                    "Structured trip plan failed "
                    "semantic validation | "
                    "attempt=%s/%s | error=%s",
                    attempt,
                    MAX_SEMANTIC_ATTEMPTS,
                    type(error).__name__,
                )

                if (
                    attempt
                    == MAX_SEMANTIC_ATTEMPTS
                ):
                    raise AIServiceError(
                        "Не удалось сформировать "
                        "логически корректный маршрут. "
                        "Попробуйте изменить запрос."
                    ) from error

                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": model_text,
                        },
                        {
                            "role": "user",
                            "content": (
                                SEMANTIC_RETRY_PROMPT
                            ),
                        },
                    ]
                )

    raise AIServiceError(
        "Не удалось сформировать маршрут."
    )