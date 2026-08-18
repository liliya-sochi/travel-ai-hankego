"""
Настройки приложения.

Все секретные данные читаются из файла .env.
Токены и пароли нельзя записывать прямо в Python-код,
потому что код позже попадёт в GitHub.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Настройки, доступные всему приложению.

    Pydantic автоматически:
    1. читает переменные из файла .env;
    2. проверяет их типы;
    3. сообщает об ошибке, если обязательного значения нет.
    """

    # Адрес OpenAI-совместимого API.
    llm_base_url: str

    # Секретный ключ доступа к выбранному AI-сервису.
    llm_api_key: str

    # Точное название модели у AI-провайдера.
    llm_model: str

    # Базовый адрес Geoapify API.
    geoapify_base_url: str = "https://api.geoapify.com"

    # Секретный ключ доступа к Geoapify.
    geoapify_api_key: SecretStr

    # Максимальная длительность одного запроса к Geoapify.
    geoapify_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=60.0,
    )

    # Токен Telegram-бота, полученный у BotFather.
    telegram_bot_token: str

    # Полный адрес FastAPI backend.
    backend_url: str = "http://127.0.0.1:8000"

    # Общий префикс HTTP-маршрутов backend.
    api_prefix: str = "/api/v1"

    # Секретный ключ между Telegram-ботом и FastAPI.
    internal_api_key: SecretStr = Field(
        min_length=32,
    )

    # Адрес Redis.
    redis_url: str = "redis://127.0.0.1:6379/0"

    # Максимальное количество генераций за одно окно.
    trip_plan_rate_limit: int = Field(
        default=10,
        ge=1,
        le=100,
    )

    # Продолжительность окна rate limit в секундах.
    trip_plan_rate_window_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
    )

    # Максимальное количество LLM-разборов сообщений за одно окно.
    trip_intake_rate_limit: int = Field(
        default=60,
        ge=1,
        le=500,
    )

    # Продолжительность окна conversational intake в секундах.
    trip_intake_rate_window_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
    )

    # Максимальное время блокировки одной генерации.
    trip_plan_lock_ttl_seconds: int = Field(
        default=180,
        ge=120,
        le=600,
    )

    # Строка подключения к PostgreSQL через asyncpg.
    database_url: str

    # Минимальный уровень сообщений в логах.
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Создаёт объект настроек один раз за время работы процесса.
    """

    return Settings()
