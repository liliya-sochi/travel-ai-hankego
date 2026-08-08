"""
Подключение приложения к PostgreSQL через SQLAlchemy.

Модуль создаёт:
1. единый AsyncEngine для всего процесса;
2. фабрику асинхронных сессий;
3. FastAPI-зависимость для выдачи Session на один запрос.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()


# Engine создаётся один раз при импорте модуля.
# Он управляет пулом подключений к PostgreSQL.
engine = create_async_engine(
    settings.database_url,
    # Проверяет соединение перед использованием.
    # Это защищает от выдачи из пула уже разорванного соединения.
    pool_pre_ping=True,
)


# Фабрика создаёт новую AsyncSession для каждого запроса.
# Сама переменная session_factory не является Session.
session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # После commit() поля ORM-объекта остаются доступными.
    # Иначе SQLAlchemy может попытаться повторно загрузить их из БД.
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Выдаёт отдельную Session на время одного запроса.

    После завершения запроса контекстный менеджер автоматически
    закрывает Session и возвращает соединение в пул Engine.
    """

    async with session_factory() as session:
        yield session
