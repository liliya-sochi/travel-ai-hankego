"""
Unit-тесты детерминированной логики conversational intake.
"""

import pytest

import app.services.trip_intake as trip_intake_service
from app.schemas.trip import (
    TripDraft,
    TripIntakeExtraction,
    TripIntent,
)
from app.services.ai import AIServiceError
from app.services.trip_intake import build_trip_intake_response


def build_extraction(
    *,
    intent: TripIntent = "plan_trip",
    destination: str | None = None,
    duration_days: int | None = None,
    travel_period: str | None = None,
    budget: str | None = None,
    interests: str | None = None,
) -> TripIntakeExtraction:
    """Создаёт полный strict Structured Output для теста."""

    return TripIntakeExtraction(
        intent=intent,
        destination=destination,
        duration_days=duration_days,
        travel_period=travel_period,
        budget=budget,
        interests=interests,
    )


def test_complete_message_is_ready_without_optional_fields() -> None:
    """Направления и длительности достаточно для генерации."""

    result = build_trip_intake_response(
        draft=TripDraft(),
        extraction=build_extraction(
            destination="Япония",
            duration_days=7,
            travel_period="Осенью",
        ),
    )

    assert result.ready_to_generate is True
    assert result.missing_required_fields == []
    assert result.next_question is None
    assert result.draft.destination == "Япония"
    assert result.draft.duration_days == 7
    assert result.draft.budget is None
    assert result.draft.interests is None


def test_only_first_missing_field_is_asked() -> None:
    """Бот задаёт один вопрос о первом обязательном пробеле."""

    result = build_trip_intake_response(
        draft=TripDraft(),
        extraction=build_extraction(
            destination="Япония",
        ),
    )

    assert result.ready_to_generate is False
    assert result.missing_required_fields == ["duration_days"]
    assert result.next_question is not None
    assert "сколько дней" in result.next_question.lower()


def test_short_answer_preserves_existing_draft() -> None:
    """Короткий ответ дополняет и не стирает старые параметры."""

    result = build_trip_intake_response(
        draft=TripDraft(
            destination="Япония",
            interests="Природа",
        ),
        extraction=build_extraction(
            duration_days=10,
        ),
    )

    assert result.ready_to_generate is True
    assert result.draft.destination == "Япония"
    assert result.draft.duration_days == 10
    assert result.draft.interests == "Природа"


@pytest.mark.parametrize(
    "intent",
    [
        "show_trips",
        "unknown",
    ],
)
def test_non_planning_intent_does_not_change_draft(
    intent: TripIntent,
) -> None:
    """Постороннее действие не изменяет параметры поездки."""

    original_draft = TripDraft(
        destination="Япония",
    )

    result = build_trip_intake_response(
        draft=original_draft,
        extraction=build_extraction(
            intent=intent,
            destination="Париж",
        ),
    )

    assert result.intent == intent
    assert result.draft == original_draft
    assert result.ready_to_generate is False


def test_cancel_clears_draft() -> None:
    """Явная отмена возвращает пустой черновик."""

    result = build_trip_intake_response(
        draft=TripDraft(
            destination="Япония",
            duration_days=7,
        ),
        extraction=build_extraction(
            intent="cancel",
        ),
    )

    assert result.intent == "cancel"
    assert result.draft == TripDraft()
    assert result.ready_to_generate is False
    assert result.next_question is None


@pytest.mark.asyncio
async def test_process_trip_message_connects_ai_and_business_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет передачу LLM-результата в бизнес-логику."""

    received_arguments: dict[str, object] = {}

    async def fake_analyze_trip_message(
        *,
        user_message: str,
        draft: TripDraft,
    ) -> TripIntakeExtraction:
        received_arguments["user_message"] = user_message
        received_arguments["draft"] = draft

        return build_extraction(
            duration_days=10,
        )

    monkeypatch.setattr(
        trip_intake_service,
        "analyze_trip_message",
        fake_analyze_trip_message,
    )

    original_draft = TripDraft(
        destination="Япония",
        interests="Природа",
    )

    result = await trip_intake_service.process_trip_message(
        user_message="Примерно на десять дней",
        draft=original_draft,
    )

    assert received_arguments == {
        "user_message": "Примерно на десять дней",
        "draft": original_draft,
    }

    assert result.intent == "plan_trip"
    assert result.ready_to_generate is True
    assert result.draft.destination == "Япония"
    assert result.draft.duration_days == 10
    assert result.draft.interests == "Природа"


@pytest.mark.asyncio
async def test_process_trip_message_propagates_ai_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверяет передачу безопасной ошибки на уровень API."""

    async def fake_analyze_trip_message(
        *,
        user_message: str,
        draft: TripDraft,
    ) -> TripIntakeExtraction:
        raise AIServiceError("Не удалось понять сообщение.")

    monkeypatch.setattr(
        trip_intake_service,
        "analyze_trip_message",
        fake_analyze_trip_message,
    )

    with pytest.raises(
        AIServiceError,
        match="Не удалось понять сообщение",
    ):
        await trip_intake_service.process_trip_message(
            user_message="Хочу куда-нибудь",
            draft=TripDraft(),
        )
