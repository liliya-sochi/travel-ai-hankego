"""
Pydantic-схемы интеграции с Geoapify.

Внешние схемы проверяют ответы провайдера.
Внутренние схемы содержат только данные, разрешённые
для использования остальными компонентами HankeGo.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GeoapifyResponseSchema(BaseModel):
    """
    Базовая схема ответа внешнего API.

    Geoapify может добавить новые поля без предупреждения,
    поэтому неизвестные поля безопасно игнорируются.
    """

    model_config = ConfigDict(
        extra="ignore",
    )


class GeoapifyLocationProperties(GeoapifyResponseSchema):
    """Свойства географического объекта Geoapify."""

    formatted: str = Field(
        min_length=1,
    )
    lat: float = Field(
        ge=-90.0,
        le=90.0,
    )
    lon: float = Field(
        ge=-180.0,
        le=180.0,
    )
    place_id: str = Field(
        min_length=1,
    )


class GeoapifyWikiAndMedia(GeoapifyResponseSchema):
    """Доступные ссылки на внешние справочные источники."""

    wikidata: str | None = Field(
        default=None,
        max_length=2000,
    )
    wikipedia: str | None = Field(
        default=None,
        max_length=2000,
    )
    wikimedia_commons: str | None = Field(
        default=None,
        max_length=2000,
    )
    image: str | None = Field(
        default=None,
        max_length=2000,
    )


class GeoapifyPlaceProperties(GeoapifyLocationProperties):
    """Свойства конкретного места из Places API."""

    name: str | None = Field(
        default=None,
        min_length=1,
    )
    categories: list[str] = Field(
        default_factory=list,
    )
    distance: float | None = Field(
        default=None,
        ge=0.0,
    )
    details: list[str] = Field(
        default_factory=list,
    )
    wiki_and_media: GeoapifyWikiAndMedia | None = None


class GeoapifyPlaceDetailsProperties(GeoapifyPlaceProperties):
    """Свойства объекта из Place Details API."""

    feature_type: str = Field(
        min_length=1,
        max_length=100,
    )
    website: str | None = Field(
        default=None,
        max_length=2000,
    )
    opening_hours: str | None = Field(
        default=None,
        max_length=1000,
    )


class GeoapifyLocationFeature(GeoapifyResponseSchema):
    """Один результат Forward Geocoding API."""

    type: Literal["Feature"]
    properties: GeoapifyLocationProperties


class GeoapifyPlaceFeature(GeoapifyResponseSchema):
    """Один результат Places API."""

    type: Literal["Feature"]
    properties: GeoapifyPlaceProperties


class GeoapifyPlaceDetailsFeature(GeoapifyResponseSchema):
    """Один объект из ответа Place Details API."""

    type: Literal["Feature"]
    properties: GeoapifyPlaceDetailsProperties


class GeoapifyGeocodingResponse(GeoapifyResponseSchema):
    """GeoJSON FeatureCollection от Forward Geocoding API."""

    type: Literal["FeatureCollection"]
    features: list[GeoapifyLocationFeature]


class GeoapifyPlacesResponse(GeoapifyResponseSchema):
    """GeoJSON FeatureCollection от Places API."""

    type: Literal["FeatureCollection"]
    features: list[GeoapifyPlaceFeature]


class GeoapifyPlaceDetailsResponse(GeoapifyResponseSchema):
    """GeoJSON FeatureCollection от Place Details API."""

    type: Literal["FeatureCollection"]
    features: list[GeoapifyPlaceDetailsFeature]


class HankeGoGeoSchema(BaseModel):
    """Базовая строгая внутренняя схема HankeGo."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


class DestinationLocation(HankeGoGeoSchema):
    """Проверенное географическое положение направления."""

    formatted_name: str = Field(
        min_length=1,
        max_length=500,
    )
    latitude: float = Field(
        ge=-90.0,
        le=90.0,
    )
    longitude: float = Field(
        ge=-180.0,
        le=180.0,
    )
    source_place_id: str = Field(
        min_length=1,
        max_length=500,
    )


class PlaceCandidate(HankeGoGeoSchema):
    """Проверенное место, которое можно передать планировщику."""

    name: str = Field(
        min_length=1,
        max_length=500,
    )
    formatted_address: str = Field(
        min_length=1,
        max_length=1000,
    )
    latitude: float = Field(
        ge=-90.0,
        le=90.0,
    )
    longitude: float = Field(
        ge=-180.0,
        le=180.0,
    )
    categories: list[str] = Field(
        default_factory=list,
    )
    distance_meters: float | None = Field(
        default=None,
        ge=0.0,
    )
    available_details: list[str] = Field(
        default_factory=list,
    )
    wiki_reference_count: int = Field(
        default=0,
        ge=0,
        le=4,
    )
    website: str | None = Field(
        default=None,
        max_length=2000,
    )
    opening_hours: str | None = Field(
        default=None,
        max_length=1000,
    )
    source_place_id: str = Field(
        min_length=1,
        max_length=500,
    )
    source: Literal["geoapify"] = "geoapify"


class PlaceDetails(HankeGoGeoSchema):
    """Проверенные дополнительные сведения о месте."""

    source_place_id: str = Field(
        min_length=1,
        max_length=500,
    )
    website: str | None = Field(
        default=None,
        max_length=2000,
    )
    opening_hours: str | None = Field(
        default=None,
        max_length=1000,
    )
    source: Literal["geoapify"] = "geoapify"


class TravelContext(HankeGoGeoSchema):
    """
    Проверенный набор актуальных данных для генерации маршрута.

    Содержит только нормализованные поля HankeGo,
    а не сырой ответ внешнего провайдера.
    """

    location: DestinationLocation
    requested_categories: list[str] = Field(
        min_length=1,
        max_length=10,
    )
    places: list[PlaceCandidate] = Field(
        min_length=1,
        max_length=20,
    )
    fetched_at: datetime
    source: Literal["geoapify"] = "geoapify"
    attribution: str = "Powered by Geoapify; data © OpenStreetMap contributors"
