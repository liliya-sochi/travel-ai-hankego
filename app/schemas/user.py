"""
Pydantic-схемы пользователей HankeGo.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TelegramUserUpsertRequest(BaseModel):
    """
    Данные Telegram-пользователя для регистрации.
    """

    model_config = ConfigDict(
        # Удаляет пробелы в начале и конце строк.
        str_strip_whitespace=True,
        # Запрещает неизвестные поля в JSON.
        extra="forbid",
    )

    telegram_id: int = Field(
        strict=True,
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    first_name: str = Field(
        strict=True,
        min_length=1,
        max_length=255,
        description="Имя пользователя из Telegram.",
        examples=["Liliya"],
    )


class UserResponse(BaseModel):
    """
    Безопасный ответ после регистрации пользователя.
    """

    model_config = ConfigDict(
        from_attributes=True,
    )

    id: int
    first_name: str
    created_at: datetime
