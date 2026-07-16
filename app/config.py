"""
Централизованная конфигурация приложения HankeGo.

Модуль хранит настройки, которые могут различаться
между локальной разработкой, тестированием и production-средой.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Описывает доступные настройки приложения.

    Pydantic проверяет типы значений и при необходимости
    загружает их из переменных окружения или файла .env.
    """

    app_name: str = "HankeGo"
    app_version: str = "0.1.0"

    # Разрешаем только три заранее определённых режима.
    # Опечатка вроде "prodution" вызовет понятную ошибку.
    environment: Literal["development", "testing", "production"] = "development"

    # Файл .env будет использоваться только локально.
    # В GitHub он не попадёт, потому что добавлен в .gitignore.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Создаём единый объект настроек для текущего приложения.
settings = Settings()