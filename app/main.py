"""
Главная точка входа FastAPI-приложения.

Здесь создаётся приложение и подключаются router-файлы.
Бизнес-логики в main.py быть не должно.
"""

from fastapi import FastAPI

from app.api.trip import router as trip_router
from app.config import get_settings


settings = get_settings()


app = FastAPI(
    title="HankeGo API",
    description="Backend AI-помощника для планирования путешествий.",
    version="0.1.0",
)


@app.get(
    "/info",
    tags=["System"],
    summary="Проверить работу backend",
)
async def get_info() -> dict[str, str]:
    """
    Простой технический endpoint.

    Он пригодится позже на сервере:
    по нему можно быстро понять, запущен ли backend.
    """

    return {
        "name": "HankeGo API",
        "status": "ok",
        "version": "0.1.0",
    }


app.include_router(
    trip_router,
    prefix=settings.api_prefix,
    tags=["Trips"],
)