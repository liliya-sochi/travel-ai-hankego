"""
Сервис для обращения к языковой модели.

Этот файл отвечает только за работу с LLM:
- создаёт инструкции для модели;
- отправляет HTTP-запрос;
- получает ответ;
- проверяет структуру ответа;
- повторяет запрос при ошибке формата.
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
MAX_FORMAT_ATTEMPTS = 2


SYSTEM_PROMPT = """
Ты — AI-помощник по планированию путешествий HankeGo.

Создай реалистичный план путешествия на основании запроса пользователя.

Верни только корректный JSON без Markdown, пояснений и блоков ```.

JSON должен иметь строго такую структуру:
{
  "destination": "Название города или страны",
  "duration_days": 5,
  "summary": "Краткое описание поездки",
  "days": [
    {
      "day": 1,
      "title": "Название и основная тема дня",
      "morning": [
        "План на утро"
      ],
      "afternoon": [
        "План на день"
      ],
      "evening": [
        "План на вечер"
      ]
    }
  ],
  "practical_tips": [
    "Первый практический совет",
    "Второй практический совет"
  ]
}

Правила:
- отвечай на русском языке;
- количество объектов в days должно соответствовать duration_days;
- не выдумывай точные цены, расписания и часы работы;
- предупреждай, что актуальные цены и расписания нужно проверять отдельно;
- составляй практичный план без чрезмерного количества мест на один день;
- для каждого дня отдельно заполняй morning, afternoon и evening;
- оставляй список пустым только тогда, когда активность действительно не нужна;
- не добавляй поля, отсутствующие в указанной JSON-структуре.
""".strip()


FORMAT_RETRY_PROMPT = """
Предыдущий ответ не прошёл проверку формата.

Верни план заново:
- только корректный JSON;
- без Markdown;
- без пояснений до или после JSON;
- строго по структуре из системной инструкции.
""".strip()


class AIServiceError(Exception):
    """
    Безопасная ошибка сервиса языковой модели.

    Детали ошибки записываются в журнал приложения,
    но не передаются пользователю API.
    """


def _extract_model_text(response_data: dict[str, Any]) -> str:
    """
    Извлекает текст модели из OpenAI-совместимого ответа.
    """

    try:
        model_text = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(
            "AI-провайдер вернул ответ неизвестной структуры."
        ) from error

    if not isinstance(model_text, str) or not model_text.strip():
        raise ValueError("AI-провайдер вернул пустой ответ.")

    return model_text


def _validate_trip_plan(model_text: str) -> TripPlanResponse:
    """
    Преобразует JSON модели и проверяет его через Pydantic.
    """

    trip_data = json.loads(model_text)
    return TripPlanResponse.model_validate(trip_data)


async def _request_model(
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
        return response.json()

    except httpx.TimeoutException as error:
        logger.warning(
            "AI provider timeout after %.1f seconds",
            REQUEST_TIMEOUT_SECONDS,
        )
        raise AIServiceError(
            "AI-сервис временно не отвечает. Попробуйте ещё раз."
        ) from error

    except httpx.HTTPStatusError as error:
        logger.error(
            "AI provider returned HTTP %s: %s",
            error.response.status_code,
            error.response.text[:1000],
        )
        raise AIServiceError(
            "AI-сервис временно недоступен. Попробуйте позже."
        ) from error

    except httpx.RequestError as error:
        logger.error(
            "AI provider connection error: %s",
            type(error).__name__,
        )
        raise AIServiceError(
            "Не удалось подключиться к AI-сервису."
        ) from error

    except json.JSONDecodeError as error:
        logger.error("AI provider returned invalid HTTP JSON.")
        raise AIServiceError(
            "AI-сервис вернул некорректный ответ."
        ) from error


async def generate_trip_plan(user_prompt: str) -> TripPlanResponse:
    """
    Создаёт и возвращает проверенный план путешествия.

    При некорректном JSON или ошибке Pydantic выполняется
    одна дополнительная попытка с уточняющей инструкцией.
    """

    settings = get_settings()

    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
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

    timeout = httpx.Timeout(
        timeout=REQUEST_TIMEOUT_SECONDS,
        connect=10.0,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, MAX_FORMAT_ATTEMPTS + 1):
            payload: dict[str, Any] = {
                "model": settings.llm_model,
                "messages": messages,
                "temperature": 0.2,
            }

            response_data = await _request_model(
                client=client,
                url=url,
                headers=headers,
                payload=payload,
            )

            try:
                model_text = _extract_model_text(response_data)
                return _validate_trip_plan(model_text)

            except (
                ValueError,
                json.JSONDecodeError,
                ValidationError,
            ) as error:
                logger.warning(
                    "Invalid model output on attempt %s/%s: %s",
                    attempt,
                    MAX_FORMAT_ATTEMPTS,
                    type(error).__name__,
                )

                if attempt == MAX_FORMAT_ATTEMPTS:
                    raise AIServiceError(
                        "Не удалось сформировать корректный маршрут. "
                        "Попробуйте изменить запрос."
                    ) from error

                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": response_data
                            .get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", ""),
                        },
                        {
                            "role": "user",
                            "content": FORMAT_RETRY_PROMPT,
                        },
                    ]
                )

    raise AIServiceError(
        "Не удалось сформировать маршрут."
    )