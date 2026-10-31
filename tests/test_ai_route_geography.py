"""Проверки локальной коррекции географии без вызовов внешних сервисов."""

from datetime import UTC, datetime

import httpx
import pytest

import app.services.ai as ai_service
from app.schemas.geoapify import DestinationLocation, PlaceCandidate, TravelContext
from app.schemas.grounded_trip import (
    GroundedActivity,
    GroundedDayPlan,
    GroundedTripPlanResponse,
)
from app.schemas.trip import TripPreferences
from app.services.ai import _validate_grounded_trip_plan
from app.services.place_geography import calculate_distance_meters


@pytest.fixture
def route_case() -> tuple[TripPreferences, TravelContext, GroundedTripPlanResponse]:
    """Берёт координаты из примера; часы и справочные признаки — тестовые."""

    place_data = [
        (
            "irene",
            "Aya İrini Kilisesi",
            41.00974,
            28.98106,
            ["building.historic", "building.tourism", "entertainment.museum"],
        ),
        (
            "islamic",
            "Türk ve İslam Eserleri Müzesi",
            41.00628,
            28.97491,
            ["entertainment.museum"],
        ),
        (
            "orient",
            "Eski Şark Eserleri Müzesi",
            41.0114495,
            28.9804625,
            ["building.tourism", "entertainment.museum"],
        ),
        ("military", "Askerî Müze", 41.0488755, 28.9882427, ["entertainment.museum"]),
        (
            "ataturk",
            "Atatürk Museum, Şişli",
            41.05640,
            28.98721,
            ["building.tourism", "entertainment.museum"],
        ),
    ]
    context = TravelContext(
        location=DestinationLocation(
            formatted_name="Стамбул",
            latitude=41.0082,
            longitude=28.9784,
            source_place_id="istanbul",
        ),
        requested_categories=["building.historic", "building.tourism"],
        places=[
            PlaceCandidate(
                source_place_id=place_id,
                name=name,
                latitude=latitude,
                longitude=longitude,
                categories=categories,
                formatted_address="Тестовый адрес, Стамбул",
                opening_hours="Mo-Su 09:00-17:00",
            )
            for place_id, name, latitude, longitude, categories in place_data
        ],
        fetched_at=datetime.now(UTC),
    )
    places = {place.source_place_id: place for place in context.places}

    def visit(place_id: str, focus: str) -> GroundedActivity:
        return GroundedActivity.model_validate(
            {
                "source_place_id": place_id,
                "place_name": places[place_id].name,
                "activity_focus": focus,
                "description": None,
            }
        )

    def walk() -> GroundedActivity:
        return GroundedActivity(
            source_place_id=None,
            place_name=None,
            activity_focus=None,
            description="Прогуляться и отдохнуть.",
        )

    plan = GroundedTripPlanResponse(
        destination="Стамбул",
        duration_days=2,
        summary="Посетить Aya İrini Kilisesi и Eski Şark Eserleri Müzesi.",
        days=[
            GroundedDayPlan(
                day=1,
                title="Исторический центр",
                morning=[visit("irene", "history")],
                afternoon=[visit("islamic", "museum")],
                evening=[walk()],
            ),
            GroundedDayPlan(
                day=2,
                title="Военный и восточный кварталы",
                morning=[visit("military", "museum")],
                afternoon=[visit("orient", "architecture")],
                evening=[walk()],
            ),
        ],
    )
    return (
        TripPreferences(
            destination="Стамбул", duration_days=2, interests="История и архитектура"
        ),
        context,
        plan,
    )


def test_replaces_distant_afternoon_and_refreshes_text(route_case) -> None:
    """Сохраняет утренние места и оба интереса в примере Стамбула."""

    preferences, context, plan = route_case
    places = {place.source_place_id: place for place in context.places}

    def distance(first: str, second: str) -> float:
        return calculate_distance_meters(
            first_latitude=places[first].latitude,
            first_longitude=places[first].longitude,
            second_latitude=places[second].latitude,
            second_longitude=places[second].longitude,
        )

    assert distance("military", "orient") > 4_000
    assert distance("military", "ataturk") < 900
    result = _validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=preferences,
        travel_context=context,
    )

    assert (
        "Atatürk Museum, Şişli: осмотреть архитектурный объект"
        in (result.days[1].afternoon[0])
    )
    assert result.days[0].title == "История и музеи"
    assert (
        "Aya İrini Kilisesi: осмотреть исторический объект"
        in (result.days[0].morning[0])
    )
    assert "Askerî Müze: посетить музей" in result.days[1].morning[0]
    assert result.summary == "Темы маршрута: история, музеи и архитектура."
    assert result.summary != plan.summary
    assert "кварталы" not in result.days[1].title
    assert "Часы по данным Geoapify" in result.days[1].afternoon[0]


@pytest.mark.parametrize(
    "blocker",
    ["required", "closed", "morning_only", "focus", "evidence", "used", "far"],
)
def test_rejects_distant_plan_when_replacement_is_not_suitable(
    route_case, blocker
) -> None:
    """Неподходящая замена не позволяет принять далёкую пару."""

    preferences, context, plan = route_case
    candidate = context.places[-1]
    if blocker == "required":
        preferences.must_visit_places = [context.places[2].name]
    elif blocker == "closed":
        candidate.opening_hours = "off"
    elif blocker == "morning_only":
        candidate.opening_hours = "Mo-Su 07:00-11:00"
    elif blocker == "focus":
        candidate.categories = ["entertainment.museum"]
    elif blocker == "evidence":
        candidate.opening_hours = None
    elif blocker == "used":
        plan.days[0].afternoon.append(
            GroundedActivity(
                source_place_id=candidate.source_place_id,
                place_name=candidate.name,
                activity_focus="museum",
                description=None,
            )
        )
    elif blocker == "far":
        candidate.latitude, candidate.longitude = 41.00137, 29.04040

    original = plan.model_dump()
    with pytest.raises(ValueError, match="distant daily places"):
        _validate_grounded_trip_plan(
            plan.model_dump_json(), preferences=preferences, travel_context=context
        )
    assert plan.model_dump() == original


def test_keeps_already_compact_day(route_case) -> None:
    """Не меняет выбранные места, если дневные расстояния уже невелики."""

    preferences, context, plan = route_case
    context.places[3].latitude = 41.012
    context.places[3].longitude = 28.982
    result = _validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=preferences,
        travel_context=context,
    )
    assert "Eski Şark Eserleri Müzesi" in result.days[1].afternoon[0]
    assert result.summary == "Темы маршрута: история, музеи и архитектура."


def test_replacement_must_be_close_to_evening_too(route_case) -> None:
    """Не переносит дневной переезд между обедом и конкретной вечерней точкой."""

    preferences, context, plan = route_case
    evening_place = context.places[2].model_copy(
        update={
            "source_place_id": "evening-park",
            "name": "Тестовый парк",
            "categories": ["leisure.park"],
            "opening_hours": "24/7",
        }
    )
    context.places.append(evening_place)
    plan.days[1].evening = [
        GroundedActivity(
            source_place_id=evening_place.source_place_id,
            place_name=evening_place.name,
            activity_focus="park",
            description=None,
        )
    ]
    with pytest.raises(ValueError, match="distant daily places"):
        _validate_grounded_trip_plan(
            plan.model_dump_json(), preferences=preferences, travel_context=context
        )


def test_rejects_distant_original_if_adjustment_fails_validation(
    route_case,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Невалидная коррекция не выпускает исходный далёкий ответ."""

    preferences, context, plan = route_case

    def invalid_adjustment(*, plan: GroundedTripPlanResponse, **_: object) -> int:
        plan.days[1].afternoon[0].source_place_id = "PRIVATE_INVALID_PLACE_ID"
        return 1

    monkeypatch.setattr(ai_service, "_shorten_grounded_afternoons", invalid_adjustment)
    with pytest.raises(ValueError, match="distant daily places"):
        _validate_grounded_trip_plan(
            plan.model_dump_json(), preferences=preferences, travel_context=context
        )
    assert "validation_reason=unknown_place_id" in caplog.text
    assert "PRIVATE_INVALID_PLACE_ID" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_generation_uses_one_http_call_and_respects_edits(
    route_case,
    monkeypatch: pytest.MonkeyPatch,
    editing: bool,
) -> None:
    """Коррекция не вызывает retry и не меняет выбор при редактировании."""

    preferences, context, plan = route_case
    plan.summary = "Маршрут включает пять обязательных парков и целый день отдыха."
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": plan.model_dump_json()},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        ai_service.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    if editing:
        current_plan = plan.to_trip_plan_response(
            practical_tips=[],
            places_by_id={place.source_place_id: place for place in context.places},
        )
        result = await ai_service.generate_edited_trip_plan(
            preferences=preferences,
            current_preferences=preferences,
            travel_context=context,
            current_plan=current_plan,
            instruction="Сохрани выбранные музеи.",
        )
        assert "Eski Şark Eserleri Müzesi" in result.days[1].afternoon[0]
    else:
        result = await ai_service.generate_trip_plan(
            preferences=preferences,
            travel_context=context,
        )
        assert "Atatürk Museum, Şişli" in result.days[1].afternoon[0]
    assert len(calls) == 1
    assert result.summary == "Темы маршрута: история, музеи и архитектура."


@pytest.mark.parametrize("required_anchor", [False, True])
def test_panorama_tiled_regression_from_live_report(required_anchor: bool) -> None:
    """Исправляет реальную пару 5,15 км, не заменяя обязательную Panorama."""
    import json
    from pathlib import Path

    corpus = json.loads(
        (Path(__file__).parents[1] / "evals/route_cases.json").read_text(
            encoding="utf-8"
        )
    )
    context = TravelContext.model_validate_json(
        json.dumps(corpus["contexts"]["istanbul"])
    )
    places = {place.source_place_id: place for place in context.places}

    def visit(place_id: str, focus: str) -> GroundedActivity:
        return GroundedActivity(
            source_place_id=place_id,
            place_name=places[place_id].name,
            activity_focus=focus,
            description=None,
        )

    def walk() -> GroundedActivity:
        return GroundedActivity(
            source_place_id=None,
            place_name=None,
            activity_focus=None,
            description="Прогулка по городу.",
        )

    plan = GroundedTripPlanResponse(
        destination="Стамбул",
        duration_days=2,
        summary="История и архитектура.",
        days=[
            GroundedDayPlan(
                day=1,
                title="История и архитектура",
                morning=[visit("eval-ist-irene", "history")],
                afternoon=[visit("eval-ist-orient", "architecture")],
                evening=[walk()],
            ),
            GroundedDayPlan(
                day=2,
                title="История и архитектура",
                morning=[visit("eval-ist-panorama", "architecture")],
                afternoon=[visit("eval-ist-tiled", "history")],
                evening=[walk()],
            ),
        ],
    )
    preferences = TripPreferences(
        destination="Стамбул",
        duration_days=2,
        interests="История и архитектура",
        must_visit_places=[places["eval-ist-panorama"].name] if required_anchor else [],
    )
    before = plan.model_dump()
    if required_anchor:
        with pytest.raises(ValueError, match="distant daily places"):
            _validate_grounded_trip_plan(
                plan.model_dump_json(), preferences=preferences, travel_context=context
            )
    else:
        result = _validate_grounded_trip_plan(
            plan.model_dump_json(), preferences=preferences, travel_context=context
        )
        assert result.days[0].morning[0].startswith("Aya İrini Kilisesi:")
        assert result.days[0].afternoon[0].startswith("Eski Şark Eserleri Müzesi:")
        assert result.days[1].afternoon[0].startswith("Çinili Köşk:")
        assert "Panorama" not in result.days[1].morning[0]
        assert "осмотреть архитектурный объект" in result.days[1].morning[0]
        selected = []
        for day in result.days:
            daily = [
                place
                for activity in (*day.morning, *day.afternoon, *day.evening)
                for place in context.places
                if activity.startswith(f"{place.name}:")
            ]
            assert len(daily) == 2
            selected.extend(place.source_place_id for place in daily)
            assert (
                calculate_distance_meters(
                    first_latitude=daily[0].latitude,
                    first_longitude=daily[0].longitude,
                    second_latitude=daily[1].latitude,
                    second_longitude=daily[1].longitude,
                )
                <= 4_000
            )
        assert len(set(selected)) == 4
    assert plan.model_dump() == before


@pytest.mark.asyncio
async def test_unrepairable_distance_uses_existing_semantic_retry(
    route_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Далёкий ответ не считается успехом и использует прежний лимит попыток."""
    import json
    from unittest.mock import AsyncMock

    preferences, context, valid = route_case
    distant = valid.model_copy(deep=True)
    ataturk = context.places[-1]
    distant.days[0].afternoon = [
        GroundedActivity(
            source_place_id=ataturk.source_place_id,
            place_name=ataturk.name,
            activity_focus="architecture",
            description=None,
        )
    ]
    replies = [distant.model_dump_json(), valid.model_dump_json()]
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": replies.pop(0)}, "finish_reason": "stop"}
                ]
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        ai_service.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handle), **kwargs
        ),
    )
    monkeypatch.setattr(ai_service.asyncio, "sleep", AsyncMock())
    result = await ai_service.generate_trip_plan(
        preferences=preferences, travel_context=context
    )
    assert len(calls) == 2
    assert "distant_daily_places" in caplog.text
    assert "4000 meters" in calls[1]["messages"][-1]["content"]
    assert "Atatürk Museum, Şişli" in result.days[1].afternoon[0]
