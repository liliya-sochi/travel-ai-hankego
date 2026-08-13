"""
Бизнес-логика диалогового сбора параметров поездки.

LLM только извлекает факты и намерение. Этот сервис объединяет
черновик, проверяет обязательные поля и выбирает следующий вопрос.
"""

from app.schemas.trip import (
    RequiredTripField,
    TripDraft,
    TripIntakeExtraction,
    TripIntakeResponse,
)
from app.services.ai import analyze_trip_message

REQUIRED_FIELD_ORDER: tuple[RequiredTripField, ...] = (
    "destination",
    "duration_days",
)

NEXT_QUESTIONS: dict[RequiredTripField, str] = {
    "destination": "Куда хотите поехать?",
    "duration_days": (
        "На сколько дней планируете поездку? "
        "Можно ответить свободно, например: на неделю."
    ),
}


def merge_trip_draft(
    draft: TripDraft,
    extraction: TripIntakeExtraction,
) -> TripDraft:
    """
    Обновляет черновик только фактами из нового сообщения.

    Значение null означает, что новой информации о поле нет.
    Поэтому уже собранное значение не должно быть удалено.
    """

    extracted_values = extraction.model_dump(
        exclude={"intent"},
        exclude_none=True,
    )

    merged_data = {
        **draft.model_dump(mode="python"),
        **extracted_values,
    }

    return TripDraft.model_validate(merged_data)


def get_missing_required_fields(
    draft: TripDraft,
) -> list[RequiredTripField]:
    """Возвращает отсутствующие обязательные поля в порядке вопросов."""

    return [
        field_name
        for field_name in REQUIRED_FIELD_ORDER
        if getattr(draft, field_name) is None
    ]


def build_trip_intake_response(
    *,
    draft: TripDraft,
    extraction: TripIntakeExtraction,
) -> TripIntakeResponse:
    """
    Объединяет данные и определяет следующий шаг диалога.
    """

    if extraction.intent == "cancel":
        return TripIntakeResponse(
            intent="cancel",
            draft=TripDraft(),
            missing_required_fields=list(REQUIRED_FIELD_ORDER),
            ready_to_generate=False,
            next_question=None,
        )

    if extraction.intent != "plan_trip":
        return TripIntakeResponse(
            intent=extraction.intent,
            draft=draft,
            missing_required_fields=get_missing_required_fields(draft),
            ready_to_generate=False,
            next_question=None,
        )

    merged_draft = merge_trip_draft(
        draft,
        extraction,
    )

    missing_fields = get_missing_required_fields(merged_draft)

    next_question = NEXT_QUESTIONS[missing_fields[0]] if missing_fields else None

    return TripIntakeResponse(
        intent="plan_trip",
        draft=merged_draft,
        missing_required_fields=missing_fields,
        ready_to_generate=not missing_fields,
        next_question=next_question,
    )


async def process_trip_message(
    *,
    user_message: str,
    draft: TripDraft,
) -> TripIntakeResponse:
    """
    Обрабатывает одну реплику пользователя.

    Сначала LLM извлекает намерение и новые факты.
    Затем обычная Python-логика определяет следующее действие.
    """

    extraction = await analyze_trip_message(
        user_message=user_message,
        draft=draft,
    )

    return build_trip_intake_response(
        draft=draft,
        extraction=extraction,
    )
