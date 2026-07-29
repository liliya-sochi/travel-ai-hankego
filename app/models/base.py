"""
Базовый класс для всех SQLAlchemy-моделей проекта.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Общая база ORM-моделей.

    Все классы таблиц HankeGo будут наследоваться от Base.
    """

    pass