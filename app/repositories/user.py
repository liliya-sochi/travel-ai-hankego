"""
Репозиторий пользователей.

Содержит только операции чтения и записи PostgreSQL.
Бизнес-логика в этом модуле не размещается.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    """
    Выполняет SQL-операции с таблицей users.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Получает SQLAlchemy-сессию текущего HTTP-запроса.
        """

        self._session = session

    async def upsert_telegram_user(
        self,
        telegram_id: int,
        first_name: str,
    ) -> User:
        """
        Создаёт пользователя или обновляет его имя.

        Операция выполняется одним атомарным SQL-запросом.
        """

        statement = (
            insert(User)
            .values(
                telegram_id=telegram_id,
                first_name=first_name,
            )
            .on_conflict_do_update(
                index_elements=[User.telegram_id],
                set_={
                    "first_name": first_name,
                },
            )
            .returning(User)
        )

        result = await self._session.execute(statement)

        return result.scalar_one()