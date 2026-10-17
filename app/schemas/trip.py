"""
Pydantic-схемы планирования путешествий.
"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MustVisitPlaceName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=500,
    ),
]


class StrictSchema(BaseModel):
    """
    Базовая строгая схема HankeGo.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


TripDayPeriod = Literal["morning", "afternoon", "evening"]
GroundedActivityFocus = Literal[
    "history",
    "architecture",
    "museum",
    "food",
    "park",
    "entertainment",
    "sight",
    "place",
]
MAX_ACTIVITIES_PER_PERIOD = 2


class TripPeriodScope(StrictSchema):
    """Период конкретного дня или всех дней текущей поездки."""

    day: int | None = Field(
        ge=1,
        le=30,
        description="Номер дня поездки или null для всех дней.",
    )
    period: TripDayPeriod


class RestPeriod(TripPeriodScope):
    """Явно запрошенный отдых в одном периоде дня или во всех днях."""


class TripPeriodAction(TripPeriodScope):
    """Сохранение периода или явно запрошенная спокойная прогулка."""

    action: Literal["preserve", "quiet_walk"]


class TripPlaceReplacement(StrictSchema):
    """Прежнее посещение, для которого явно требуется выбрать другое место."""

    day: int = Field(ge=1, le=30)
    period: TripDayPeriod
    place_name: MustVisitPlaceName = Field(
        description="Название заменяемого места из current_trip_plan, не нового места.",
    )
    activity_focus: GroundedActivityFocus | None = Field(
        description="Явно запрошенный тип нового посещения или null без такого условия.",
    )
    nearby: bool = Field(
        description="true только при явной просьбе подобрать замену рядом с прежним местом.",
    )


class RestPeriodChange(RestPeriod):
    """Одно явное добавление или отмена отдыха из текущей инструкции."""

    rest: bool = Field(description="true — запланировать отдых, false — отменить его.")


def _validate_rest_period_list(periods: list[RestPeriod]) -> list[RestPeriod]:
    """Не допускает повторов и пересечения общего правила с отдельным днём."""

    keys = {(item.day, item.period) for item in periods}
    if len(keys) != len(periods):
        raise ValueError("Периоды отдыха не должны повторяться.")
    if any(item.day is not None and (None, item.period) in keys for item in periods):
        raise ValueError("Общее правило отдыха уже охватывает указанный день.")
    return periods


class TripPreferences(StrictSchema):
    """
    Проверенные параметры поездки, достаточные для генерации маршрута.

    Направление и длительность обязательны.
    Период, бюджет и интересы используются, только если пользователь
    сам сообщил их в свободном тексте.
    """

    destination: str = Field(
        min_length=2,
        max_length=255,
        description="Страна, город или регион поездки.",
        examples=["Стамбул"],
    )

    duration_days: int = Field(
        ge=1,
        le=30,
        description="Продолжительность поездки в днях.",
        examples=[5],
    )

    travel_period: str | None = Field(
        default=None,
        min_length=2,
        max_length=255,
        description="Даты, месяц, сезон или другой период поездки.",
        examples=["В октябре 2026 года"],
    )

    budget: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Необязательный бюджет поездки в свободной форме.",
        examples=["150000 ₽"],
    )

    interests: str | None = Field(
        default=None,
        min_length=2,
        max_length=1000,
        description="Необязательные интересы и пожелания путешественника.",
        examples=["Архитектура, местная еда и прогулки"],
    )

    must_visit_places: list[MustVisitPlaceName] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Конкретные места, которые пользователь явно потребовал включить в маршрут."
        ),
        examples=[["Военный музей Стамбула"]],
    )

    rest_periods: list[RestPeriod] = Field(
        default_factory=list,
        max_length=90,
        description="Периоды отдыха, сохранённые при редактировании поездки.",
    )

    @field_validator("rest_periods")
    @classmethod
    def validate_rest_period_list(cls, periods: list[RestPeriod]) -> list[RestPeriod]:
        return _validate_rest_period_list(periods)

    def is_rest_period(self, day: int, period: TripDayPeriod) -> bool:
        """Проверяет правило для конкретного дня с учётом общих пожеланий."""

        return any(
            item.period == period and item.day in (None, day)
            for item in self.rest_periods
        )

    def concrete_activity_capacity(self, day: int) -> int:
        """Считает верхнюю границу посещений вне периодов отдыха."""

        return sum(
            MAX_ACTIVITIES_PER_PERIOD
            for period in ("morning", "afternoon", "evening")
            if not self.is_rest_period(day, period)
        )

    def rest_periods_after(self, changes: list[RestPeriodChange]) -> list[RestPeriod]:
        """Применяет изменения, сохраняя остальные правила и исключения по дням."""

        periods: tuple[TripDayPeriod, ...] = ("morning", "afternoon", "evening")
        active = {
            (day, period)
            for day in range(1, self.duration_days + 1)
            for period in periods
            if self.is_rest_period(day, period)
        }
        # Правило отдельного дня уточняет общее правило той же инструкции.
        for change in sorted(changes, key=lambda item: item.day is not None):
            if change.day is not None and change.day > self.duration_days:
                raise ValueError("День отдыха выходит за пределы поездки.")
            days = (
                range(1, self.duration_days + 1) if change.day is None else [change.day]
            )
            for day in days:
                key = (day, change.period)
                if change.rest:
                    active.add(key)
                else:
                    active.discard(key)
        result: list[RestPeriod] = []
        for period in periods:
            days = [
                day
                for day in range(1, self.duration_days + 1)
                if (day, period) in active
            ]
            if len(days) == self.duration_days:
                result.append(RestPeriod(day=None, period=period))
            else:
                result.extend(RestPeriod(day=day, period=period) for day in days)
        return result

    @model_validator(mode="after")
    def validate_rest_capacity(self) -> Self:
        """Согласует номера дней и обязательные места с периодами отдыха."""

        if any(
            item.day is not None and item.day > self.duration_days
            for item in self.rest_periods
        ):
            raise ValueError("День отдыха выходит за пределы поездки.")
        capacity = sum(
            self.concrete_activity_capacity(day)
            for day in range(1, self.duration_days + 1)
        )
        if len(self.must_visit_places) > capacity:
            raise ValueError("Для обязательных мест не осталось периодов посещения.")
        return self


class TripDraft(StrictSchema):
    """
    Неполный черновик поездки между сообщениями пользователя.

    Все поля необязательны, потому что пользователь может сообщать
    параметры постепенно и в произвольном порядке.
    """

    destination: str | None = Field(
        default=None,
        min_length=2,
        max_length=255,
    )

    duration_days: int | None = Field(
        default=None,
        ge=1,
        le=30,
    )

    travel_period: str | None = Field(
        default=None,
        min_length=2,
        max_length=255,
    )

    budget: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )

    interests: str | None = Field(
        default=None,
        min_length=2,
        max_length=1000,
    )

    must_visit_places: list[MustVisitPlaceName] = Field(
        default_factory=list,
        max_length=5,
    )


TripIntent = Literal[
    "plan_trip",
    "show_trips",
    "cancel",
    "unknown",
]

RequiredTripField = Literal[
    "destination",
    "duration_days",
]


class TripIntakeExtraction(StrictSchema):
    """
    Строгий Structured Output распознавания сообщения пользователя.

    Каждое поле обязательно должно присутствовать в JSON,
    но может содержать null, если новое сообщение не содержит
    соответствующей информации.
    """

    intent: TripIntent = Field(
        description="Распознанное намерение пользователя.",
    )

    destination: str | None = Field(
        min_length=2,
        max_length=255,
        description="Полное новое значение направления или null.",
    )

    duration_days: int | None = Field(
        ge=1,
        le=30,
        description="Полное новое значение длительности или null.",
    )

    travel_period: str | None = Field(
        min_length=2,
        max_length=255,
        description="Полное новое значение периода поездки или null.",
    )

    budget: str | None = Field(
        min_length=1,
        max_length=255,
        description="Полное новое значение бюджета или null.",
    )

    interests: str | None = Field(
        min_length=2,
        max_length=1000,
        description="Полное новое значение интересов или null.",
    )

    must_visit_places: list[MustVisitPlaceName] | None = Field(
        max_length=5,
        description=(
            "Конкретные места, которые пользователь явно потребовал "
            "включить в маршрут, или null."
        ),
    )


class TripIntakeRequest(StrictSchema):
    """
    Запрос на разбор одного сообщения в диалоге о поездке.
    """

    telegram_id: int = Field(
        gt=0,
        description="Идентификатор пользователя для rate limiting.",
    )

    user_message: str = Field(
        min_length=1,
        max_length=2000,
        description="Одно недоверенное текстовое сообщение пользователя.",
    )

    draft: TripDraft = Field(
        default_factory=TripDraft,
        description="Уже собранные параметры текущего диалога.",
    )


class TripIntakeResponse(StrictSchema):
    """
    Детерминированный результат обработки сообщения.
    """

    intent: TripIntent
    draft: TripDraft
    missing_required_fields: list[RequiredTripField]
    ready_to_generate: bool
    next_question: str | None


class TripEditAnalysis(StrictSchema):
    """Structured Output разбора инструкции редактирования."""

    supported: bool = Field(
        description=(
            "Можно ли выполнить изменение без смены направления "
            "или продолжительности поездки."
        ),
    )

    interests_changed: bool = Field(
        description="Изменяет ли инструкция интересы и стиль поездки.",
    )

    interests: str | None = Field(
        min_length=2,
        max_length=1000,
        description=(
            "Полное новое значение интересов, null для очистки "
            "или при отсутствии изменения."
        ),
    )

    must_visit_places_changed: bool = Field(
        description="Изменяет ли инструкция список конкретных обязательных мест.",
    )

    must_visit_places: list[MustVisitPlaceName] = Field(
        max_length=5,
        description=(
            "Полный новый список обязательных мест или текущий список, "
            "если он не изменился."
        ),
    )

    rest_period_changes: list[RestPeriodChange] = Field(
        max_length=90,
        description="Только явно запрошенные добавления и отмены отдыха; [] без изменений.",
    )

    period_actions: list[TripPeriodAction] = Field(
        max_length=90,
        description="Явные просьбы сохранить период или сделать спокойную прогулку.",
    )
    requires_regeneration: bool = Field(
        description="Нужна ли генерация для изменений, не покрытых отдыхом и period_actions.",
    )
    regeneration_periods: list[TripPeriodScope] = Field(
        max_length=90,
        description=(
            "Только периоды, которые разрешено изменить генератору; "
            "day=null — все дни. [] без генерации. Остальные периоды сохраняются."
        ),
    )
    place_replacements: list[TripPlaceReplacement] = Field(
        max_length=180,
        description=(
            "Прежние конкретные посещения, которые нужно заменить другими местами; "
            "[] при отсутствии такой просьбы. Не является списком обязательных мест."
        ),
    )

    @field_validator("place_replacements")
    @classmethod
    def validate_place_replacements(
        cls, replacements: list[TripPlaceReplacement]
    ) -> list[TripPlaceReplacement]:
        keys = {
            (item.day, item.period, " ".join(item.place_name.casefold().split()))
            for item in replacements
        }
        if len(keys) != len(replacements):
            raise ValueError("Заменяемые посещения не должны повторяться.")
        return replacements

    @field_validator("regeneration_periods")
    @classmethod
    def validate_regeneration_periods(
        cls, periods: list[TripPeriodScope]
    ) -> list[TripPeriodScope]:
        if len({(item.day, item.period) for item in periods}) != len(periods):
            raise ValueError("Периоды генерации не должны повторяться.")
        return periods

    @field_validator("period_actions")
    @classmethod
    def validate_period_actions(
        cls, actions: list[TripPeriodAction]
    ) -> list[TripPeriodAction]:
        if len({(item.day, item.period) for item in actions}) != len(actions):
            raise ValueError("Действия одного периода не должны повторяться.")
        return actions

    @model_validator(mode="after")
    def validate_local_edit(self) -> Self:
        if self.supported and self.requires_regeneration != bool(
            self.regeneration_periods
        ):
            raise ValueError("Режим генерации должен соответствовать её периодам.")
        if not self.requires_regeneration and (
            self.interests_changed
            or self.must_visit_places_changed
            or self.place_replacements
        ):
            raise ValueError(
                "Изменение интересов, обязательных мест или замена посещения требует генерации."
            )
        return self

    @field_validator("rest_period_changes")
    @classmethod
    def validate_rest_changes(
        cls, changes: list[RestPeriodChange]
    ) -> list[RestPeriodChange]:
        if len({(item.day, item.period) for item in changes}) != len(changes):
            raise ValueError("Изменения одного периода отдыха не должны повторяться.")
        return changes


class TripPlanRequest(StrictSchema):
    """
    Запрос на создание и сохранение маршрута.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    first_name: str = Field(
        min_length=1,
        max_length=255,
        description="Имя пользователя Telegram.",
        examples=["Liliya"],
    )

    preferences: TripPreferences = Field(
        description="Проверенные параметры будущей поездки.",
    )


class TripEditRequest(StrictSchema):
    """Запрос изменения существующего маршрута."""

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
    )

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    instruction: str = Field(
        min_length=1,
        max_length=2000,
        description="Одно текстовое требование к изменению маршрута.",
    )


class TripHistoryRequest(StrictSchema):
    """
    Запрос истории маршрутов пользователя.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=20,
        description="Максимальное количество маршрутов.",
    )


class TripDetailsRequest(StrictSchema):
    """
    Запрос полного сохранённого маршрута.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
        examples=[7],
    )


class TripDeleteRequest(StrictSchema):
    """
    Запрос удаления сохранённого маршрута.
    """

    telegram_id: int = Field(
        gt=0,
        description="Уникальный идентификатор пользователя Telegram.",
        examples=[9000000001],
    )

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
        examples=[7],
    )


class DayPlan(StrictSchema):
    """
    План одного дня путешествия.
    """

    day: int = Field(
        ge=1,
        description="Номер дня, начиная с единицы.",
    )

    title: str = Field(
        min_length=1,
        max_length=255,
        description="Краткая тема дня.",
    )

    morning: list[str] = Field(
        description="План на утро.",
    )

    afternoon: list[str] = Field(
        description="План на день.",
    )

    evening: list[str] = Field(
        description="План на вечер.",
    )


class TripPlanResponse(StrictSchema):
    """
    Проверенный Structured Output языковой модели.
    """

    destination: str = Field(
        min_length=1,
        max_length=255,
        description="Город или страна назначения.",
    )

    duration_days: int = Field(
        ge=1,
        le=30,
        description="Продолжительность поездки в днях.",
    )

    summary: str = Field(
        min_length=1,
        description="Краткое описание путешествия.",
    )

    days: list[DayPlan] = Field(
        min_length=1,
        description="Подробный план по дням.",
    )

    practical_tips: list[str] = Field(
        description="Практические советы.",
    )

    @model_validator(mode="after")
    def validate_days(self) -> Self:
        """
        Проверяет количество и последовательность дней.
        """

        if len(self.days) != self.duration_days:
            raise ValueError(
                "Количество элементов days должно соответствовать duration_days."
            )

        actual_days = [day.day for day in self.days]

        expected_days = list(range(1, self.duration_days + 1))

        if actual_days != expected_days:
            raise ValueError(
                "Номера дней должны идти последовательно от 1 до duration_days."
            )

        return self


class TripCreateResponse(TripPlanResponse):
    """
    Сохранённый маршрут, возвращаемый клиенту.
    """

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    created_at: datetime = Field(
        description="Время сохранения маршрута.",
    )


class TripDetailsResponse(TripPlanResponse):
    """
    Полный сохранённый маршрут пользователя.
    """

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    created_at: datetime = Field(
        description="Время сохранения маршрута.",
    )

    editable: bool = Field(
        description="Доступны ли исходные параметры для редактирования.",
    )


class TripEditResponse(TripDetailsResponse):
    """Обновлённый и сохранённый маршрут."""

    updated: Literal[True] = Field(
        default=True,
        description="Подтверждение успешного сохранения изменений.",
    )


class TripDeleteResponse(StrictSchema):
    """
    Результат успешного удаления маршрута.
    """

    trip_id: int = Field(
        gt=0,
        description="Идентификатор удалённого маршрута.",
    )

    deleted: Literal[True] = Field(
        default=True,
        description="Подтверждение успешного удаления.",
    )


class TripSummaryResponse(StrictSchema):
    """
    Краткая информация о сохранённом маршруте.
    """

    trip_id: int = Field(
        gt=0,
        description="Внутренний идентификатор маршрута.",
    )

    destination: str = Field(
        min_length=1,
        max_length=255,
        description="Направление поездки.",
    )

    duration_days: int = Field(
        ge=1,
        le=30,
        description="Продолжительность поездки.",
    )

    created_at: datetime = Field(
        description="Время сохранения маршрута.",
    )


class TripHistoryResponse(StrictSchema):
    """
    История маршрутов пользователя.
    """

    count: int = Field(
        ge=0,
        description="Количество возвращённых маршрутов.",
    )

    trips: list[TripSummaryResponse] = Field(
        default_factory=list,
        description="Последние сохранённые маршруты.",
    )
