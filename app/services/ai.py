"""
Сервис для обращения к языковой модели.

Этот файл отвечает только за работу с LLM:
- создаёт инструкции для модели;
- отправляет HTTP-запрос;
- получает ответ;
- проверяет структуру ответа.
"""

import json

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.schemas.trip import TripPlanResponse


# Системная инструкция задаёт модели постоянную роль
# и требует вернуть только JSON определённой структуры.
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
      "title": "Название дня",
      "activities": [
        "Первое занятие",
        "Второе занятие"
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
- составляй практичный план без чрезмерного количества мест на один день.
""".strip()


class AIServiceError(Exception):
    """
    Ошибка, возникшая во время работы с языковой моделью.

    Собственное исключение позволяет router не зависеть
    от деталей httpx, JSON и конкретного AI-провайдера.
    """


async def generate_trip_plan(user_prompt: str) -> TripPlanResponse:
    """
    Отправляет запрос пользователя языковой модели
    и возвращает проверенный план путешествия.

    async означает, что во время ожидания ответа модели
    FastAPI не блокирует всё приложение и может
    обслуживать другие запросы.
    """

    settings = get_settings()

    # Формируем полный адрес endpoint.
    # rstrip("/") удаляет возможный слеш в конце base URL,
    # чтобы не получить адрес вида //chat/completions.
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"

    headers = {
        # Bearer — стандартный способ передачи API-ключа
        # во многих OpenAI-совместимых сервисах.
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": settings.llm_model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],

        # Низкая температура делает ответ менее случайным
        # и помогает модели стабильнее соблюдать JSON-формат.
        "temperature": 0.3,
    }

    try:
        # AsyncClient используется для асинхронных HTTP-запросов.
        # timeout=60 означает, что мы готовы ждать ответ
        # языковой модели не более 60 секунд.
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                url=url,
                headers=headers,
                json=payload,
            )

        # Если сервер вернул 400, 401, 404, 500 и подобную ошибку,
        # httpx создаст исключение HTTPStatusError.
        response.raise_for_status()

    except httpx.TimeoutException as error:
        raise AIServiceError(
            "Языковая модель не ответила за 60 секунд."
        ) from error

    except httpx.HTTPStatusError as error:
        # Тело ответа часто содержит полезное описание:
        # неверный ключ, неизвестная модель или недостаток средств.
        provider_message = error.response.text

        raise AIServiceError(
            f"AI-сервис вернул ошибку: {provider_message}"
        ) from error

    except httpx.RequestError as error:
        raise AIServiceError(
            "Не удалось подключиться к AI-сервису."
        ) from error

    try:
        # Преобразуем JSON-ответ всего HTTP-запроса
        # в обычный Python-словарь.
        response_data = response.json()

        # OpenAI-совместимые API обычно возвращают текст модели
        # по этому пути:
        # choices -> первый элемент -> message -> content.
        model_text = response_data["choices"][0]["message"]["content"]

        # Модель возвращает JSON как обычную строку.
        # json.loads превращает строку JSON в Python-словарь.
        trip_data = json.loads(model_text)

        # Pydantic проверяет, что ответ модели действительно
        # соответствует TripPlanResponse.
        return TripPlanResponse.model_validate(trip_data)

    except (
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise AIServiceError(
            "Модель вернула ответ в неправильном формате."
        ) from error