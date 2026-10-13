"""Проверки сокращённого контекста редактирования без потери активностей."""

import json
from datetime import UTC, datetime

import pytest

from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.grounded_trip import GroundedTripPlanResponse
from app.schemas.trip import TripPlanResponse, TripPreferences
from app.services.ai import (
    _build_grounded_edit_user_message,
    _build_grounded_user_message,
    _build_trip_edit_analysis_user_message,
    _compact_saved_activity,
    _compact_trip_plan_for_edit,
)


def build_saved_plan(duration: int = 1) -> tuple[TripPlanResponse, TravelContext]:
    """Формирует настоящий публичный текст из проверенных данных места."""

    place = PlaceCandidate(
        name="Museum: 本館",
        localized_name="Музей: главный корпус",
        formatted_address="PRIVATE_ADDRESS, Tokyo, Japan",
        latitude=35.68,
        longitude=139.76,
        categories=["entertainment.museum"],
        source_place_id="museum-id",
        opening_hours="Mo-Su 09:00-17:00",
        opening_hours_source="google",
        location_source="google",
        website="https://example.invalid/PRIVATE_WEBSITE",
    )
    context = TravelContext(
        location=DestinationLocation(
            formatted_name="Токио",
            latitude=35.68,
            longitude=139.76,
            source_place_id="tokyo-id",
        ),
        requested_categories=["entertainment.museum"],
        places=[place],
        fetched_at=datetime.now(UTC),
    )
    visit = {
        "source_place_id": place.source_place_id,
        "place_name": place.name,
        "activity_focus": "museum",
        "description": None,
    }
    rest = {
        "source_place_id": None,
        "place_name": None,
        "activity_focus": None,
        "description": "Спокойный отдых.",
    }
    grounded = GroundedTripPlanResponse(
        destination="Токио",
        duration_days=duration,
        summary="План поездки",
        days=[
            {
                "day": day,
                "title": "План дня",
                "morning": [visit, visit],
                "afternoon": [visit],
                "evening": [rest],
            }
            for day in range(1, duration + 1)
        ],
    )
    return grounded.to_trip_plan_response(
        places_by_id={place.source_place_id: place},
        practical_tips=["PRIVATE_TIP"],
    ), context


@pytest.mark.parametrize("duration", [1, 30])
def test_keeps_all_days_periods_and_actions_without_reference_data(
    duration: int,
) -> None:
    """Длинная поездка сохраняется полностью; убирается только повторный текст."""

    saved, _ = build_saved_plan(duration)
    original = saved.model_dump_json()
    compact = _compact_trip_plan_for_edit(saved)
    expected_visit = "Музей: главный корпус (Museum: 本館): посетить музей."

    assert compact == {
        "destination": "Токио",
        "duration_days": duration,
        "days": [
            {
                "day": day,
                "morning": [expected_visit, expected_visit],
                "afternoon": [expected_visit],
                "evening": ["Спокойный отдых."],
            }
            for day in range(1, duration + 1)
        ],
    }
    assert saved.model_dump_json() == original
    assert (
        len(json.dumps(compact, ensure_ascii=False).encode())
        < len(original.encode()) * 0.6
    )


@pytest.mark.parametrize(
    "activity",
    [
        "Спокойный отдых.",
        "Посидеть с книгой, потом спокойно поужинать.",
        "Музей: коллекция искусства",
        "Посещение музея. Часы по данным Geoapify: уточните перед поездкой.",
        "Название Часы по данным Geoapify: особая коллекция: посетить музей.",
        "Музей: посетить музей. Затем встретиться с друзьями.",
    ],
)
def test_leaves_general_and_legacy_activities_intact(activity: str) -> None:
    """Не обрезает произвольные описания, названия и дополнительные действия."""

    assert _compact_saved_activity(activity) == activity


@pytest.mark.parametrize(
    "reference",
    [
        "Часы по данным Geoapify: пн–вс: 09:00–17:00.",
        "Часы по данным Google Maps: пн–вс: 09:00–17:00.",
        "Сайт из данных Geoapify: https://example.invalid/",
        "Сайт из данных Google Maps: https://example.invalid/",
        "Актуальный адрес по данным Google Maps: Tokyo, Japan.",
    ],
)
def test_removes_only_formatter_reference_suffix(reference: str) -> None:
    """Все поддержанные источники оставляют точное название и действие."""

    visit = "Название: главный корпус: посетить музей."
    assert _compact_saved_activity(f"{visit} {reference}") == visit


def test_both_edit_stages_receive_compact_plan_and_unchanged_constraints() -> None:
    """Разбор и генерация не теряют инструкцию, обязательные места или отдых."""

    saved, context = build_saved_plan()
    preferences = TripPreferences(
        destination="Токио",
        duration_days=1,
        interests="Музеи",
        must_visit_places=["Museum: 本館"],
        rest_periods=[{"day": None, "period": "evening"}],
    )
    instruction = "Оставь утренний музей. Вечером — спокойный отдых."
    analysis = json.loads(
        _build_trip_edit_analysis_user_message(
            current_preferences=preferences,
            current_plan=saved,
            instruction=instruction,
        )
    )
    generation = json.loads(
        _build_grounded_edit_user_message(
            preferences=preferences,
            travel_context=context,
            current_plan=saved,
            instruction=instruction,
        )
    )
    creation = json.loads(
        _build_grounded_user_message(preferences=preferences, travel_context=context)
    )

    assert analysis["current_preferences"] == preferences.model_dump(mode="json")
    assert generation["trip_preferences"] == preferences.model_dump(mode="json")
    assert generation["travel_context"] == creation["travel_context"]
    assert generation["travel_context"]["must_visit_place_ids"] == ["museum-id"]
    assert analysis["current_trip_plan"] == generation["current_trip_plan"]
    for payload in (analysis, generation):
        assert payload["edit_instruction"] == instruction
        text = json.dumps(payload, ensure_ascii=False)
        assert "PRIVATE_ADDRESS" not in text
        assert "PRIVATE_WEBSITE" not in text
        assert "PRIVATE_TIP" not in text
        assert "Museum: 本館" in text
        assert payload["current_trip_plan"]["days"][0]["evening"] == [
            "Спокойный отдых."
        ]
