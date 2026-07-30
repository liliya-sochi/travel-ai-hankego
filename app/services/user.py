"""
Бизнес-логика регистрации пользователей.
"""

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.user import UserRepository


class UserServiceError(Exception):
    """
    Ошибка бизнес-операции с пользователем.
    """


class UserService:
    """
    Управляет регистрацией пользователей HankeGo.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Создаёт сервис для одной SQLAlchemy-сессии.
        """

        self._session = session
        self._repository = UserRepository(session)

    async def register_telegram_user(
        self,
        telegram_id: int,
        first_name: str,
    ) -> User:
        """
        Создаёт пользователя или обновляет его актуальное имя.
        """

        try:
            user = await self._repository.upsert_telegram_user(
                telegram_id=telegram_id,
                first_name=first_name,
            )

            await self._session.commit()

        except SQLAlchemyError as error:
            await self._session.rollback()

            raise UserServiceError(
                "Не удалось сохранить пользователя."
            ) from error

        return user