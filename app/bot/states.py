"""
Состояния диалога Telegram-бота при планировании поездки.
"""

from aiogram.fsm.state import State, StatesGroup


class TripPlanning(StatesGroup):
    """
    Этапы сбора информации для создания маршрута.
    """

    # Бот ожидает направление поездки.
    destination = State()

    # Бот ожидает продолжительность поездки.
    duration = State()

    # Бот ожидает бюджет.
    budget = State()

    # Бот ожидает интересы пользователя.
    interests = State()
