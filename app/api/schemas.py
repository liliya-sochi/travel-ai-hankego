"""
Pydantic-модели API.

Используются для проверки входящих
и исходящих данных.
"""

from pydantic import BaseModel


class EchoRequest(BaseModel):
    """
    Запрос для тестового метода echo.
    """

    message: str


class EchoResponse(BaseModel):
    """
    Ответ тестового метода echo.
    """

    message: str