"""Pydantic-схемы минимального ответа Google Places API (New)."""

from pydantic import BaseModel, ConfigDict, Field


class GooglePlacesResponseSchema(BaseModel):
    """Игнорирует новые поля, которые провайдер может добавить позже."""

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )


class GoogleLocalizedText(GooglePlacesResponseSchema):
    """Локализованное название места."""

    text: str = Field(min_length=1, max_length=500)


class GoogleLocation(GooglePlacesResponseSchema):
    """Координаты результата Google Places."""

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class GooglePeriodPoint(GooglePlacesResponseSchema):
    """Начало или конец периода работы."""

    day: int = Field(ge=0, le=6)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)


class GoogleOpeningPeriod(GooglePlacesResponseSchema):
    """Один непрерывный период работы места."""

    open: GooglePeriodPoint
    close: GooglePeriodPoint | None = None


class GoogleOpeningHours(GooglePlacesResponseSchema):
    """Обычное недельное расписание места."""

    periods: list[GoogleOpeningPeriod] = Field(default_factory=list)


class GooglePlace(GooglePlacesResponseSchema):
    """Минимальные поля результата, нужные HankeGo."""

    id: str = Field(min_length=1, max_length=500)
    display_name: GoogleLocalizedText = Field(alias="displayName")
    location: GoogleLocation
    regular_opening_hours: GoogleOpeningHours | None = Field(
        default=None,
        alias="regularOpeningHours",
    )


class GoogleTextSearchResponse(GooglePlacesResponseSchema):
    """Ответ Text Search (New)."""

    places: list[GooglePlace] = Field(default_factory=list)
