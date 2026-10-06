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
    assert result.days[0].title == plan.days[0].title
    assert (
        "Aya İrini Kilisesi: осмотреть исторический объект"
        in (result.days[0].morning[0])
    )
    assert "Askerî Müze: посетить музей" in result.days[1].morning[0]
    assert "Eski Şark" not in result.summary
    assert "Atatürk Museum, Şişli" in result.summary
    assert "кварталы" not in result.days[1].title
    assert "Часы по данным Geoapify" in result.days[1].afternoon[0]


@pytest.mark.parametrize(
    "blocker",
    ["required", "closed", "morning_only", "focus", "evidence", "used", "far"],
)
def test_keeps_valid_plan_when_replacement_is_not_suitable(route_case, blocker) -> None:
    """Запрет замены не превращает уже корректный ответ в ошибку."""

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

    result = _validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=preferences,
        travel_context=context,
    )

    assert "Eski Şark Eserleri Müzesi" in result.days[1].afternoon[0]
    assert result.summary == plan.summary
    assert result.days[1].title == plan.days[1].title


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
    assert result.summary == plan.summary


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
    result = _validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=preferences,
        travel_context=context,
    )
    assert "Eski Şark Eserleri Müzesi" in result.days[1].afternoon[0]


def test_falls_back_to_valid_plan_if_adjustment_fails_validation(
    route_case,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Повторная локальная проверка защищает исходный валидный ответ."""

    preferences, context, plan = route_case

    def invalid_adjustment(*, plan: GroundedTripPlanResponse, **_: object) -> int:
        plan.days[1].afternoon[0].source_place_id = "PRIVATE_INVALID_PLACE_ID"
        return 1

    monkeypatch.setattr(ai_service, "_shorten_grounded_afternoons", invalid_adjustment)
    result = _validate_grounded_trip_plan(
        plan.model_dump_json(),
        preferences=preferences,
        travel_context=context,
    )
    assert "Eski Şark Eserleri Müzesi" in result.days[1].afternoon[0]
    assert result.summary == plan.summary
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
