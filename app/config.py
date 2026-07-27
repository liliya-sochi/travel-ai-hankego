"""
Настройки приложения.

Все секретные данные читаются из файла .env.
Токены и пароли нельзя записывать прямо в Python-код,
потому что код позже попадёт в GitHub.
"""

from functools import lru_cache

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

    # Токен Telegram-бота, полученный у BotFather.
    telegram_bot_token: str

    # Полный адрес FastAPI backend.
    backend_url: str = "http://127.0.0.1:8000"

    # Общий префикс HTTP-маршрутов backend.
    api_prefix: str = "/api/v1"

    # Адрес Redis для хранения состояний Telegram-бота.
    redis_url: str = "redis://127.0.0.1:6379/0"

    # Минимальный уровень сообщений в логах.
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        # Имя файла с переменными окружения.
        env_file=".env",

        # Кодировка для корректного чтения русских символов.
        env_file_encoding="utf-8",

        # Разрешает заглавные имена переменных в .env.
        case_sensitive=False,

        # Неизвестные значения не ломают приложение.
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Создаёт объект настроек один раз за время работы процесса.
    """

    return Settings()