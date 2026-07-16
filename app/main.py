"""
Главная точка входа в приложение HankeGo.

Модуль создаёт экземпляр FastAPI и регистрирует
первые HTTP-маршруты приложения.
"""

from fastapi import FastAPI

from app.config import settings


# Создаём приложение, используя централизованные настройки.
# Теперь название и версия не дублируются в разных файлах.
app = FastAPI(
    title=settings.app_name,
    description="AI-powered travel platform",
    version=settings.app_version,
)


@app.get("/")
async def root() -> dict[str, str]:
    """
    Возвращает основную информацию о проекте.

    Маршрут пока используется как простая стартовая
    точка API и подтверждает, что приложение доступно.
    """

    return {
        "project": settings.app_name,
        "version": settings.app_version,
        "status": "running",
    }


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Возвращает техническое состояние приложения.

    В будущем этот маршрут будет проверять доступность
    базы данных, Redis и других зависимостей HankeGo.
    """

    return {
        "status": "healthy",
        "environment": settings.environment,
    }