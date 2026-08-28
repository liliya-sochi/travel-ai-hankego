"""Общие географические расчёты для туристических мест."""

import math

from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
)

GEOGRAPHIC_CELL_SIZE_METERS = 2_000
METERS_PER_LATITUDE_DEGREE = 111_320


def calculate_place_grid_cell(
    *,
    place: PlaceCandidate,
    location: DestinationLocation,
) -> tuple[int, int]:
    """Определяет двухкилометровую ячейку вокруг центра направления."""

    latitude_meters = (place.latitude - location.latitude) * METERS_PER_LATITUDE_DEGREE

    longitude_difference = (place.longitude - location.longitude + 180) % 360 - 180
    longitude_meters = (
        longitude_difference
        * METERS_PER_LATITUDE_DEGREE
        * math.cos(math.radians(location.latitude))
    )

    half_cell_size = GEOGRAPHIC_CELL_SIZE_METERS / 2

    return (
        math.floor((longitude_meters + half_cell_size) / GEOGRAPHIC_CELL_SIZE_METERS),
        math.floor((latitude_meters + half_cell_size) / GEOGRAPHIC_CELL_SIZE_METERS),
    )


def format_place_area_group(
    *,
    place: PlaceCandidate,
    location: DestinationLocation,
) -> str:
    """Возвращает стабильную метку географической группы для LLM."""

    longitude_cell, latitude_cell = calculate_place_grid_cell(
        place=place,
        location=location,
    )

    return f"area:{longitude_cell}:{latitude_cell}"
