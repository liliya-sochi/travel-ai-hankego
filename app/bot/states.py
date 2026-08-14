"""
Состояния разговорного планирования поездки.
"""

from aiogram.fsm.state import State, StatesGroup


class TripPlanning(StatesGroup):
    """
    Состояние активного диалога о поездке.

    Параметры поездки не разделены на отдельные состояния.
    Единый TripDraft хранится в Redis и обновляется после каждого сообщения.
    """

    collecting = State()
