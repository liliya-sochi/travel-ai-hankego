"""
ORM-модель сохранённого маршрута HankeGo.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Trip(Base):
    """
    Сгенерированный и сохранённый маршрут пользователя.
    """

    __tablename__ = "trips"

    __table_args__ = (
        CheckConstraint(
            "duration_days BETWEEN 1 AND 30",
            name="ck_trips_duration_days",
        ),
    )

    # Внутренний идентификатор маршрута.
    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    # Пользователь, которому принадлежит маршрут.
    user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    # Направление вынесено в отдельную колонку
    # для быстрого отображения списка поездок.
    destination: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Продолжительность маршрута в днях.
    duration_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # Полный проверенный Structured Output от LLM.
    plan_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    # Исходные предпочтения позволяют заново получить актуальные
    # данные мест перед безопасным редактированием маршрута.
    # Для маршрутов, созданных до появления функции, значение равно null.
    preferences_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # Время сохранения маршрута устанавливает PostgreSQL.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
