"""Unit-тесты географической группировки туристических мест."""

import math

import pytest

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
)
from app.services.place_geography import (
    EARTH_RADIUS_METERS,
    calculate_distance_meters,
    calculate_place_grid_cell,
    format_place_area_group,
)


def build_location() -> DestinationLocation:
    """Создаёт центр тестового направления."""

    return DestinationLocation(
        formatted_name="Стамбул, Турция",
        latitude=41.0082,
        longitude=28.9784,
        source_place_id="istanbul-place-id",
    )


def build_place(
    *,
    latitude: float,
    longitude: float,
) -> PlaceCandidate:
    """Создаёт место с заданными координатами."""

    return PlaceCandidate(
        name="Тестовое место",
        formatted_address="Стамбул, Турция",
        latitude=latitude,
        longitude=longitude,
        categories=["tourism.sights"],
        source_place_id=f"place:{latitude}:{longitude}",
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (41.0082, 28.9784),
        (41.0160, 28.9784),
        (41.0004, 28.9784),
        (41.0082, 28.9878),
        (41.0082, 28.9690),
    ],
)
def test_groups_places_within_one_kilometer_around_center(
    *,
    latitude: float,
    longitude: float,
) -> None:
    """Не разделяет близкие места из-за знака смещения от центра."""

    location = build_location()
    place = build_place(
        latitude=latitude,
        longitude=longitude,
    )

    assert calculate_place_grid_cell(
        place=place,
        location=location,
    ) == (0, 0)
    assert (
        format_place_area_group(
            place=place,
            location=location,
        )
        == "area:0:0"
    )


def test_assigns_distant_place_to_another_area() -> None:
    """Отделяет место, расположенное дальше центральной ячейки."""

    location = build_location()
    place = build_place(
        latitude=41.0352,
        longitude=28.9784,
    )

    assert calculate_place_grid_cell(
        place=place,
        location=location,
    ) == (0, 2)
    assert (
        format_place_area_group(
            place=place,
            location=location,
        )
        == "area:0:2"
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"), [(0.0, 0.0), (90.0, 180.0), (41.0082, 28.9784)]
)
def test_identical_coordinates_have_zero_distance(
    latitude: float, longitude: float
) -> None:
    """Одинаковые координаты не создают искусственное расстояние."""
    assert (
        calculate_distance_meters(
            first_latitude=latitude,
            first_longitude=longitude,
            second_latitude=latitude,
            second_longitude=longitude,
        )
        == 0.0
    )


@pytest.mark.parametrize(
    ("first_latitude", "first_longitude", "second_latitude", "second_longitude"),
    [
        (0.0, 0.0, 0.0, 180.0),
        (90.0, 0.0, -90.0, 0.0),
        (66.01301490014168, 155.02966763235167, -66.01301490460911, -24.97033235988793),
    ],
)
def test_antipodal_distance_is_finite_and_bounded(
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> None:
    """Противоположные точки не вызывают math domain error из-за округления."""
    forward = calculate_distance_meters(
        first_latitude=first_latitude,
        first_longitude=first_longitude,
        second_latitude=second_latitude,
        second_longitude=second_longitude,
    )
    backward = calculate_distance_meters(
        first_latitude=second_latitude,
        first_longitude=second_longitude,
        second_latitude=first_latitude,
        second_longitude=first_longitude,
    )
    assert math.isfinite(forward)
    assert 0 <= forward <= math.pi * EARTH_RADIUS_METERS
    assert forward == pytest.approx(math.pi * EARTH_RADIUS_METERS, abs=0.5)
    assert backward == pytest.approx(forward, abs=0.5)


@pytest.mark.parametrize(
    ("first_longitude", "second_longitude", "expected_meters"),
    [(0.0, 1.0, 111194.9266446), (179.99, -179.99, 2223.8985329)],
)
def test_equatorial_distance_including_date_line(
    first_longitude: float,
    second_longitude: float,
    expected_meters: float,
) -> None:
    """Обычная дуга и короткий переход через меридиан 180° сохраняют длину."""
    distance = calculate_distance_meters(
        first_latitude=0.0,
        first_longitude=first_longitude,
        second_latitude=0.0,
        second_longitude=second_longitude,
    )
    assert distance == pytest.approx(expected_meters, abs=0.001)
