"""
ORM-модель пользователя HankeGo.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """
    Пользователь HankeGo, зарегистрированный через Telegram.
    """

    __tablename__ = "users"

    # Внутренний идентификатор пользователя в базе HankeGo.
    id: Mapped[int] = mapped_column(primary_key=True)

    # Уникальный идентификатор пользователя Telegram.
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True,
        nullable=False,
    )

    # Имя используется для обращения к пользователю.
    first_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Время создания записи устанавливает сама PostgreSQL.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
