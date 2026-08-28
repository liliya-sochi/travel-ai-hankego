"""Unit-тесты географической группировки туристических мест."""

import pytest

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
)
from app.services.place_geography import (
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
