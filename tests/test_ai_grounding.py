"""Unit-тесты grounding маршрута реальными местами."""

import json
from datetime import UTC, datetime

import pytest

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    TravelContext,
)
from app.schemas.trip import TripPreferences
from app.services.ai import (
    _build_grounded_user_message,
    _validate_grounded_trip_plan,
)


def build_preferences(
    *,
    duration_days: int = 1,
) -> TripPreferences:
    """Создаёт тестовые параметры поездки."""

    return TripPreferences(
        destination="Стамбул",
        duration_days=duration_days,
        interests="История",
    )


def build_travel_context() -> TravelContext:
    """Создаёт проверенный контекст Geoapify."""

    return TravelContext(
        location=DestinationLocation(
            formatted_name="Стамбул, Турция",
            latitude=41.0082,
            longitude=28.9784,
            source_place_id="istanbul-place-id",
        ),
        requested_categories=[
            "tourism.sights",
            "entertainment.museum",
        ],
        places=[
            PlaceCandidate(
                name="Айя-София",
                formatted_address="Султанахмет, Стамбул",
                latitude=41.0086,
                longitude=28.9802,
                categories=["tourism.sights"],
                source_place_id="hagia-sophia-id",
                website="https://museum.example/",
                opening_hours="Mo-Su 09:00-18:00",
            )
        ],
        fetched_at=datetime.now(UTC),
    )


def build_grounded_plan() -> dict[str, object]:
    """Создаёт корректный grounded-ответ LLM."""

    return {
        "destination": "Стамбул",
        "duration_days": 1,
        "summary": "Исторический день в Стамбуле.",
        "days": [
            {
                "day": 1,
                "title": "Исторический центр",
                "morning": [
                    {
                        "source_place_id": ("hagia-sophia-id"),
                        "place_name": "Айя-София",
                        "description": ("Осмотреть здание и его интерьеры."),
                    }
                ],
                "afternoon": [
                    {
                        "source_place_id": None,
                        "place_name": None,
                        "description": ("Прогуляться по историческому центру."),
                    }
                ],
                "evening": [
                    {
                        "source_place_id": None,
                        "place_name": None,
                        "description": "Отдохнуть в местном кафе.",
                    }
                ],
            }
        ],
    }


def test_builds_grounded_user_message() -> None:
    """Не передаёт LLM сайты и часы работы."""

    preferences = build_preferences()
    context = build_travel_context()

    message = json.loads(
        _build_grounded_user_message(
            preferences=preferences,
            travel_context=context,
        )
    )

    assert message["trip_preferences"] == (preferences.model_dump(mode="json"))

    llm_places = message["travel_context"]["places"]

    assert len(llm_places) == 1
    assert llm_places[0]["source_place_id"] == ("hagia-sophia-id")
    assert llm_places[0]["area_group"] == "area:0:0"
    assert "website" not in llm_places[0]
    assert "opening_hours" not in llm_places[0]
    assert message["travel_context"]["geographic_planning"] == {
        "area_group_size_meters": 2000,
        "target_area_count": 1,
    }


def test_builds_multiday_geographic_planning_target() -> None:
    """Передаёт LLM достижимое число географических зон."""

    context = build_travel_context()
    context.places.extend(
        [
            PlaceCandidate(
                name="Северный музей",
                formatted_address="Шишли, Стамбул",
                latitude=41.0442,
                longitude=28.9784,
                categories=["entertainment.museum"],
                source_place_id="north-museum-id",
            ),
            PlaceCandidate(
                name="Восточный музей",
                formatted_address="Кадыкёй, Стамбул",
                latitude=41.0082,
                longitude=29.0260,
                categories=["entertainment.museum"],
                source_place_id="east-museum-id",
            ),
        ]
    )

    message = json.loads(
        _build_grounded_user_message(
            preferences=build_preferences(
                duration_days=5,
            ),
            travel_context=context,
        )
    )

    area_groups = {place["area_group"] for place in message["travel_context"]["places"]}

    assert area_groups == {
        "area:0:0",
        "area:0:2",
        "area:2:0",
    }
    assert message["travel_context"]["geographic_planning"]["target_area_count"] == 3


def test_validates_and_converts_grounded_plan() -> None:
    """Проверяет валидный ID и преобразование в старый контракт."""

    result = _validate_grounded_trip_plan(
        json.dumps(
            build_grounded_plan(),
            ensure_ascii=False,
        ),
        preferences=build_preferences(),
        travel_context=build_travel_context(),
    )

    assert result.destination == "Стамбул"
    assert result.duration_days == 1
    assert result.days[0].morning == [
        (
            "Айя-София: Осмотреть здание и его интерьеры. "
            "Часы по данным Geoapify: пн–вс: 09:00–18:00. "
            "Сайт из данных Geoapify: https://museum.example/"
        )
    ]
    assert result.days[0].afternoon == ["Прогуляться по историческому центру."]
    assert result.days[0].evening == ["Отдохнуть в местном кафе."]
    assert result.practical_tips == [
        (
            "Часы работы и ссылки на сайты получены "
            "из данных Geoapify/OSM и могут быть устаревшими; "
            "проверяйте их перед посещением."
        ),
        (
            "Точные цены и расписания могут измениться; "
            "проверяйте их непосредственно перед поездкой."
        ),
        ("Powered by Geoapify; data © OpenStreetMap contributors"),
    ]


def test_rejects_unknown_place_id() -> None:
    """Проверяет запрет места вне TravelContext."""

    plan = build_grounded_plan()
    days = plan["days"]

    assert isinstance(days, list)
    assert isinstance(days[0], dict)

    morning = days[0]["morning"]

    assert isinstance(morning, list)
    assert isinstance(morning[0], dict)

    morning[0]["source_place_id"] = "invented-id"

    with pytest.raises(
        ValueError,
        match="outside travel context",
    ):
        _validate_grounded_trip_plan(
            json.dumps(plan, ensure_ascii=False),
            preferences=build_preferences(),
            travel_context=build_travel_context(),
        )


def test_rejects_wrong_name_for_valid_id() -> None:
    """Проверяет подмену имени при существующем ID."""

    plan = build_grounded_plan()
    days = plan["days"]

    assert isinstance(days, list)
    assert isinstance(days[0], dict)

    morning = days[0]["morning"]

    assert isinstance(morning, list)
    assert isinstance(morning[0], dict)

    morning[0]["place_name"] = "Выдуманный музей"

    with pytest.raises(
        ValueError,
        match="does not match its place ID",
    ):
        _validate_grounded_trip_plan(
            json.dumps(plan, ensure_ascii=False),
            preferences=build_preferences(),
            travel_context=build_travel_context(),
        )


def test_rejects_llm_generated_practical_tips() -> None:
    """Не разрешает LLM самостоятельно добавлять советы."""

    plan = build_grounded_plan()
    plan["practical_tips"] = ["Непроверенный дресс-код."]

    with pytest.raises(ValueError):
        _validate_grounded_trip_plan(
            json.dumps(plan, ensure_ascii=False),
            preferences=build_preferences(),
            travel_context=build_travel_context(),
        )


@pytest.mark.parametrize(
    "period_name",
    [
        "morning",
        "afternoon",
        "evening",
    ],
)
def test_rejects_empty_day_period(
    period_name: str,
) -> None:
    """Не разрешает оставлять часть дня без активностей."""

    plan = build_grounded_plan()
    days = plan["days"]

    assert isinstance(days, list)
    assert isinstance(days[0], dict)

    days[0][period_name] = []

    with pytest.raises(
        ValueError,
        match="at least 1 item",
    ):
        _validate_grounded_trip_plan(
            json.dumps(plan, ensure_ascii=False),
            preferences=build_preferences(),
            travel_context=build_travel_context(),
        )


def test_rejects_more_than_two_activities_per_period() -> None:
    """Ограничивает перегрузку одного периода дня."""

    plan = build_grounded_plan()
    days = plan["days"]

    assert isinstance(days, list)
    assert isinstance(days[0], dict)

    morning = days[0]["morning"]

    assert isinstance(morning, list)

    morning.extend(
        [
            {
                "source_place_id": None,
                "place_name": None,
                "description": "Прогуляться по площади.",
            },
            {
                "source_place_id": None,
                "place_name": None,
                "description": "Осмотреть архитектуру района.",
            },
        ]
    )

    with pytest.raises(
        ValueError,
        match="at most 2 items",
    ):
        _validate_grounded_trip_plan(
            json.dumps(plan, ensure_ascii=False),
            preferences=build_preferences(),
            travel_context=build_travel_context(),
        )


def test_does_not_add_details_to_general_activity() -> None:
    """Не связывает общую активность с данными конкретного места."""

    result = _validate_grounded_trip_plan(
        json.dumps(
            build_grounded_plan(),
            ensure_ascii=False,
        ),
        preferences=build_preferences(),
        travel_context=build_travel_context(),
    )

    assert result.days[0].afternoon == ["Прогуляться по историческому центру."]


def test_adds_separator_before_place_details() -> None:
    """Отделяет описание LLM от проверенных сведений Python."""

    plan = build_grounded_plan()
    days = plan["days"]

    assert isinstance(days, list)
    assert isinstance(days[0], dict)

    morning = days[0]["morning"]

    assert isinstance(morning, list)
    assert isinstance(morning[0], dict)

    morning[0]["description"] = "Посетить музей"

    result = _validate_grounded_trip_plan(
        json.dumps(
            plan,
            ensure_ascii=False,
        ),
        preferences=build_preferences(),
        travel_context=build_travel_context(),
    )

    assert result.days[0].morning == [
        (
            "Айя-София: Посетить музей. "
            "Часы по данным Geoapify: пн–вс: 09:00–18:00. "
            "Сайт из данных Geoapify: https://museum.example/"
        )
    ]
