"""
HTTP-клиент Telegram-бота для обращения к backend HankeGo.

Модуль изолирует сетевые запросы от Telegram-обработчиков.
"""

import httpx

from app.config import settings


async def get_project_info() -> dict[str, str]:
    """
    Получает общую информацию о HankeGo из backend API.

    Таймаут ограничивает ожидание ответа, чтобы бот
    не зависал бесконечно при недоступном backend.
    """

    url = f"{settings.backend_url}/api/v1/info"

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)

        # Если backend вернул ошибку 4xx или 5xx,
        # httpx создаст исключение вместо тихого продолжения.
        response.raise_for_status()

        return response.json()
    

async def send_echo(message: str) -> dict[str, str]:
    """
    Отправляет сообщение в backend и получает его обратно.
    """

    url = f"{settings.backend_url}/api/v1/echo"

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(
            url,
            json={"message": message},
        )

        response.raise_for_status()

        return response.json()