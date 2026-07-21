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
    # Например:
    # https://api.openai.com/v1
    # https://openrouter.ai/api/v1
    llm_base_url: str

    # Секретный ключ доступа к выбранному AI-сервису.
    llm_api_key: str

    # Точное название модели, которое понимает AI-сервис.
    llm_model: str

    # Токен Telegram-бота, полученный у BotFather.
    telegram_bot_token: str

    # Полный адрес нашего FastAPI backend.
    # Пока бот и backend работают на одном компьютере.
    backend_url: str = "http://127.0.0.1:8000"

    # Общий префикс HTTP-маршрутов нашего backend.
    api_prefix: str = "/api/v1"

    model_config = SettingsConfigDict(
        # Имя файла, из которого читаются настройки.
        env_file=".env",

        # Кодировка необходима для корректного чтения
        # русских символов в .env.
        env_file_encoding="utf-8",

        # Позволяет писать переменные в .env заглавными буквами:
        # LLM_API_KEY вместо llm_api_key.
        case_sensitive=False,

        # Неизвестные значения в .env не будут ломать приложение.
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Создаёт объект настроек только один раз.

    Без lru_cache новый объект Settings создавался бы
    при каждом вызове функции. Здесь это не нужно,
    потому что настройки во время работы приложения
    не изменяются.
    """

    return Settings()