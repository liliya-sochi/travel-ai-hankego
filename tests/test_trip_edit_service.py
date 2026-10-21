"""Unit-тесты orchestration редактирования сохранённого маршрута."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.trip as trip_service_module
from app.schemas.geoapify import (
    DestinationLocation,
    PlaceCandidate,
    TravelContext,
)
from app.schemas.trip import (
    RestPeriod,
    RestPeriodChange,
    TripEditAnalysis,
    TripPlanResponse,
    TripPreferences,
)
from app.services.trip import (
    TripEditUnavailableError,
    TripEditUnsupportedError,
    TripService,
)
from scripts import evaluate_routes as evaluation


def build_plan() -> TripPlanResponse:
    """Создаёт текущую или обновлённую версию маршрута."""

    return TripPlanResponse(
        destination="Стамбул",
        duration_days=1,
        summary="Маршрут по Стамбулу.",
        days=[
            {
                "day": 1,
                "title": "Исторический центр",
                "morning": ["Прогулка"],
                "afternoon": ["Музей"],
                "evening": ["Отдых"],
            }
        ],
        practical_tips=["Проверяйте расписание."],
    )


class FakeSession:
    """Считает завершения читающей и записывающей транзакций."""

    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


class FakeUserRepository:
    """Возвращает владельца маршрута."""

    async def get_by_telegram_id(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(id=11)


class FakeEnrichmentService:
    """Возвращает свежий фиксированный контекст."""

    def __init__(self) -> None:
        self.received_preferences: TripPreferences | None = None
        self.context = TravelContext(
            location=DestinationLocation(
                formatted_name="Стамбул, Турция",
                latitude=41.0082,
                longitude=28.9784,
                source_place_id="istanbul-id",
            ),
            requested_categories=["tourism.sights"],
            places=[
                PlaceCandidate(
                    name="Айя-София",
                    formatted_address="Султанахмет, Стамбул",
                    latitude=41.0086,
                    longitude=28.9802,
                    categories=["tourism.sights"],
                    source_place_id="hagia-sophia-id",
                )
            ],
            fetched_at=datetime.now(UTC),
        )

    async def enrich(self, preferences: TripPreferences) -> TravelContext:
        self.received_preferences = preferences
        return self.context


class FakeTripRepository:
    """Хранит исходные и обновлённые данные в памяти."""

    def __init__(self, *, preferences_data: dict[str, object] | None) -> None:
        self.trip = SimpleNamespace(
            id=7,
            user_id=11,
            plan_data=build_plan().model_dump(mode="json"),
            preferences_data=preferences_data,
            created_at=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
        )
        self.update_arguments: dict[str, object] | None = None

    async def get_by_id_and_user_id(self, **_: Any) -> SimpleNamespace:
        return self.trip

    async def update_by_id_and_user_id(
        self,
        **arguments: Any,
    ) -> SimpleNamespace:
        self.update_arguments = arguments
        return self.trip


@pytest.mark.asyncio
@pytest.mark.parametrize("nearby", [False, True])
async def test_scoped_generation_requests_fresh_preserved_visits_without_promoting_them(
    monkeypatch,
    nearby,
):
    suite = evaluation.load_edit_suite()
    case = suite.cases[2]
    old = suite.plans[case.current_plan]
    session = FakeSession()
    repository = FakeTripRepository(
        preferences_data=case.preferences.model_dump(mode="json")
    )
    repository.trip.plan_data = old.model_dump(mode="json")
    parsed = TripEditAnalysis(
        supported=True,
        interests_changed=False,
        interests=None,
        must_visit_places_changed=False,
        must_visit_places=case.preferences.must_visit_places,
        rest_period_changes=[],
        period_actions=[],
        place_removals=[],
        place_additions=[],
        place_replacements=[
            {
                "day": 2,
                "period": "afternoon",
                "place_name": "Тестовый музей 4",
                "activity_focus": "museum",
                "nearby": nearby,
            }
        ],
        requires_regeneration=True,
        regeneration_periods=[{"day": 2, "period": "afternoon"}],
    )

    async def analyze(**_):
        return parsed

    async def generate(**arguments):
        assert arguments["analysis"] == parsed
        return old

    class Enrichment:
        async def enrich(self, preferences, *, preserved_place_names):
            assert set(preserved_place_names) == {
                "Art Aquarium Museum GINZA",
                "Mitsuo Aida Museum",
                "Тестовый музей 3",
            } | ({"Тестовый музей 4"} if nearby else set())
            assert preferences.must_visit_places == ["Art Aquarium Museum GINZA"]
            return suite.contexts[case.context]

    monkeypatch.setattr(trip_service_module, "analyze_trip_edit", analyze)
    monkeypatch.setattr(trip_service_module, "generate_edited_trip_plan", generate)
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository
    await service.edit_trip_plan(
        telegram_id=9000000001,
        trip_id=7,
        instruction=case.steps[0].user_message,
        enrichment_service=Enrichment(),
    )
    assert repository.update_arguments["preferences_data"]["must_visit_places"] == [
        "Art Aquarium Museum GINZA"
    ]
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_unrelated_edit_preserves_saved_rest_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Редактирование интересов не отменяет ранее сохранённый отдых."""

    original = TripPreferences(
        destination="Стамбул",
        duration_days=1,
        rest_periods=[RestPeriod(day=None, period="evening")],
    )

    async def analyze(**_: Any) -> TripEditAnalysis:
        return TripEditAnalysis(
            period_actions=[],
            place_removals=[],
            place_additions=[],
            place_replacements=[],
            requires_regeneration=True,
            regeneration_periods=[
                {"day": None, "period": period}
                for period in ("morning", "afternoon", "evening")
            ],
            supported=True,
            interests_changed=True,
            interests="Музеи",
            must_visit_places_changed=False,
            must_visit_places=[],
            rest_period_changes=[],
        )

    async def generate(**arguments: Any) -> TripPlanResponse:
        assert arguments["preferences"].is_rest_period(1, "evening")
        return build_plan()

    monkeypatch.setattr(trip_service_module, "analyze_trip_edit", analyze)
    monkeypatch.setattr(trip_service_module, "generate_edited_trip_plan", generate)
    session = FakeSession()
    repository = FakeTripRepository(preferences_data=original.model_dump(mode="json"))
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository
    await service.edit_trip_plan(
        telegram_id=9000000001,
        trip_id=7,
        instruction="Добавь интерес к музеям.",
        enrichment_service=FakeEnrichmentService(),
    )
    assert repository.update_arguments is not None
    assert repository.update_arguments["preferences_data"]["rest_periods"] == [
        {"day": None, "period": "evening"}
    ]
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_rest_conflict_leaves_saved_trip_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отдых во всех периодах не удаляет обязательное место из сохранённого плана."""

    original = TripPreferences(
        destination="Стамбул", duration_days=1, must_visit_places=["Айя-София"]
    )

    async def analyze(**_: Any) -> TripEditAnalysis:
        return TripEditAnalysis(
            period_actions=[],
            place_removals=[],
            place_additions=[],
            place_replacements=[],
            requires_regeneration=True,
            regeneration_periods=[
                {"day": None, "period": period}
                for period in ("morning", "afternoon", "evening")
            ],
            supported=True,
            interests_changed=False,
            interests=None,
            must_visit_places_changed=False,
            must_visit_places=["Айя-София"],
            rest_period_changes=[
                RestPeriodChange(day=None, period=period, rest=True)
                for period in ("morning", "afternoon", "evening")
            ],
        )

    async def unexpected_generation(**_: Any) -> TripPlanResponse:
        pytest.fail("Конфликт должен быть обнаружен до генерации.")

    monkeypatch.setattr(trip_service_module, "analyze_trip_edit", analyze)
    monkeypatch.setattr(
        trip_service_module, "generate_edited_trip_plan", unexpected_generation
    )
    session = FakeSession()
    repository = FakeTripRepository(preferences_data=original.model_dump(mode="json"))
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository
    enrichment = FakeEnrichmentService()
    with pytest.raises(TripEditUnsupportedError, match="Периоды отдыха несовместимы"):
        await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction="Весь день только отдых.",
            enrichment_service=enrichment,
        )
    assert repository.update_arguments is None
    assert repository.trip.preferences_data == original.model_dump(mode="json")
    assert session.commit_count == 0
    assert enrichment.received_preferences is None


@pytest.mark.asyncio
async def test_edit_rebuilds_context_and_saves_full_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Изменяет preferences, обогащает их заново и делает один commit."""

    original_preferences = TripPreferences(
        destination="Стамбул",
        duration_days=1,
        interests="Архитектура",
    )
    analysis = TripEditAnalysis(
        period_actions=[],
        place_removals=[],
        place_additions=[],
        place_replacements=[],
        requires_regeneration=True,
        regeneration_periods=[
            {"day": None, "period": period}
            for period in ("morning", "afternoon", "evening")
        ],
        supported=True,
        interests_changed=True,
        interests="Архитектура и музеи",
        must_visit_places_changed=True,
        rest_period_changes=[RestPeriodChange(day=None, period="evening", rest=True)],
        must_visit_places=["Айя-София"],
    )
    captured: dict[str, object] = {}

    async def fake_analyze_trip_edit(**arguments: Any) -> TripEditAnalysis:
        captured["analysis_arguments"] = arguments
        return analysis

    async def fake_generate_edited_trip_plan(
        *,
        preferences: TripPreferences,
        current_preferences: TripPreferences,
        travel_context: TravelContext,
        current_plan: TripPlanResponse,
        instruction: str,
        analysis: TripEditAnalysis,
    ) -> TripPlanResponse:
        captured["generation_preferences"] = preferences
        captured["generation_current_preferences"] = current_preferences
        captured["generation_context"] = travel_context
        captured["current_plan"] = current_plan
        captured["instruction"] = instruction
        return build_plan()

    monkeypatch.setattr(
        trip_service_module,
        "analyze_trip_edit",
        fake_analyze_trip_edit,
    )
    monkeypatch.setattr(
        trip_service_module,
        "generate_edited_trip_plan",
        fake_generate_edited_trip_plan,
    )

    session = FakeSession()
    repository = FakeTripRepository(
        preferences_data=original_preferences.model_dump(mode="json"),
    )
    enrichment_service = FakeEnrichmentService()
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository

    result = await service.edit_trip_plan(
        telegram_id=9000000001,
        trip_id=7,
        instruction="Добавь Айя-Софию. Вечером спокойный отдых.",
        enrichment_service=enrichment_service,
    )

    expected_preferences = TripPreferences(
        destination="Стамбул",
        duration_days=1,
        interests="Архитектура и музеи",
        must_visit_places=["Айя-София"],
        rest_periods=[RestPeriod(day=None, period="evening")],
    )
    assert enrichment_service.received_preferences == expected_preferences
    assert captured["generation_preferences"] == expected_preferences
    assert captured["generation_current_preferences"].rest_periods == []
    assert captured["instruction"] == "Добавь Айя-Софию. Вечером спокойный отдых."
    assert repository.update_arguments is not None
    assert repository.update_arguments["preferences_data"] == (
        expected_preferences.model_dump(mode="json")
    )
    assert session.rollback_count == 1
    assert session.commit_count == 1
    assert result.trip_id == 7
    assert result.updated is True


@pytest.mark.asyncio
async def test_edit_rejects_legacy_trip_before_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не угадывает preferences для старого сохранённого маршрута."""

    async def unexpected_analysis(**_: Any) -> None:
        pytest.fail("LLM не должна вызываться для старого маршрута")

    monkeypatch.setattr(
        trip_service_module,
        "analyze_trip_edit",
        unexpected_analysis,
    )
    session = FakeSession()
    service = TripService(session)  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = FakeTripRepository(preferences_data=None)

    with pytest.raises(TripEditUnavailableError, match="создан до"):
        await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction="Замени вечер",
            enrichment_service=FakeEnrichmentService(),
        )

    assert session.commit_count == 0


@pytest.mark.asyncio
async def test_edit_rejects_destination_or_duration_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Смена поездки останавливается до enrichment и генерации."""

    async def fake_analyze_trip_edit(**_: Any) -> TripEditAnalysis:
        return TripEditAnalysis(
            period_actions=[],
            place_removals=[],
            place_additions=[],
            place_replacements=[],
            requires_regeneration=True,
            regeneration_periods=[
                {"day": None, "period": period}
                for period in ("morning", "afternoon", "evening")
            ],
            supported=False,
            interests_changed=False,
            interests=None,
            must_visit_places_changed=False,
            rest_period_changes=[],
            must_visit_places=[],
        )

    monkeypatch.setattr(
        trip_service_module,
        "analyze_trip_edit",
        fake_analyze_trip_edit,
    )
    enrichment_service = FakeEnrichmentService()
    preferences = TripPreferences(destination="Стамбул", duration_days=1)
    service = TripService(FakeSession())  # type: ignore[arg-type]
    service._user_repository = FakeUserRepository()
    service._trip_repository = FakeTripRepository(
        preferences_data=preferences.model_dump(mode="json"),
    )

    with pytest.raises(TripEditUnsupportedError, match="количество дней"):
        await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction="Сделай три дня в Риме",
            enrichment_service=enrichment_service,
        )

    assert enrichment_service.received_preferences is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_new", [False, True])
async def test_named_addition_fresh_lookup_and_atomic_service_write(
    monkeypatch, missing_new
):
    """Сервис проверяет старое место добавляемого периода и пишет только полный результат."""

    from app.services.ai import AIServiceError

    suite = evaluation.load_edit_suite()
    case = suite.cases[3]
    plan = suite.plans[case.current_plan]
    context = suite.contexts[case.context].model_copy(deep=True)
    if missing_new:
        context.places = [
            p for p in context.places if p.source_place_id != "eval-edit-museum-5"
        ]
    parsed = TripEditAnalysis(
        supported=True,
        interests_changed=False,
        interests=None,
        must_visit_places_changed=True,
        must_visit_places=["Art Aquarium Museum GINZA", "Тестовый музей 5"],
        rest_period_changes=[],
        period_actions=[],
        requires_regeneration=False,
        regeneration_periods=[],
        place_replacements=[],
        place_removals=[],
        place_additions=[
            {
                "day": 2,
                "period": "afternoon",
                "place_name": "Тестовый музей 5",
                "activity_focus": "museum",
            }
        ],
    )

    async def analyze(**_):
        return parsed

    class Enrichment:
        async def enrich(self, preferences, *, preserved_place_names):
            assert set(preserved_place_names) == {
                "Art Aquarium Museum GINZA",
                "Mitsuo Aida Museum",
                "Тестовый музей 3",
                "Тестовый музей 4",
            }
            assert preferences.must_visit_places == parsed.must_visit_places
            return context

    session = FakeSession()
    repository = FakeTripRepository(
        preferences_data=case.preferences.model_dump(mode="json")
    )
    repository.trip.plan_data = plan.model_dump(mode="json")
    monkeypatch.setattr(trip_service_module, "analyze_trip_edit", analyze)
    service = TripService(session)
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository

    async def edit():
        return await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction=case.steps[0].user_message,
            enrichment_service=Enrichment(),
        )

    if missing_new:
        with pytest.raises(AIServiceError):
            await edit()
        assert repository.update_arguments is None
        assert session.commit_count == 0
    else:
        await edit()
        saved = repository.update_arguments
        assert len(saved["plan_data"]["days"][1]["afternoon"]) == 2
        assert (
            saved["preferences_data"]["must_visit_places"] == parsed.must_visit_places
        )
        assert session.commit_count == 1
    assert repository.trip.plan_data == plan.model_dump(mode="json")
    assert repository.trip.preferences_data == case.preferences.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect", [None, "removed_missing", "kept_missing", "empty_period"]
)
async def test_named_removal_lookup_and_atomic_write(monkeypatch, defect):
    """Удаляемое место не обогащается; неподтверждённый сосед отменяет запись."""

    from app.services.ai import AIServiceError

    suite = evaluation.load_edit_suite()
    case = suite.cases[4]
    plan = suite.plans[case.current_plan].model_copy(deep=True)
    context = suite.contexts[case.context].model_copy(deep=True)
    if defect in {"removed_missing", "kept_missing"}:
        missing = (
            "eval-edit-museum-5"
            if defect == "removed_missing"
            else "eval-edit-museum-4"
        )
        context.places = [p for p in context.places if p.source_place_id != missing]
    if defect == "empty_period":
        plan.days[1].afternoon = plan.days[1].afternoon[1:]
    parsed = TripEditAnalysis(
        supported=True,
        interests_changed=False,
        interests=None,
        must_visit_places_changed=True,
        must_visit_places=["Art Aquarium Museum GINZA"],
        rest_period_changes=[],
        period_actions=[],
        requires_regeneration=False,
        regeneration_periods=[],
        place_replacements=[],
        place_additions=[],
        place_removals=[
            {"day": 2, "period": "afternoon", "place_name": "Тестовый музей 5"}
        ],
    )

    async def analyze(**_):
        return parsed

    class Enrichment:
        called = False

        async def enrich(self, preferences, *, preserved_place_names):
            self.called = True
            assert set(preserved_place_names) == {
                "Art Aquarium Museum GINZA",
                "Mitsuo Aida Museum",
                "Тестовый музей 3",
                "Тестовый музей 4",
            }
            assert preferences.must_visit_places == ["Art Aquarium Museum GINZA"]
            return context

    enrichment = Enrichment()
    session = FakeSession()
    repository = FakeTripRepository(
        preferences_data=case.preferences.model_dump(mode="json")
    )
    repository.trip.plan_data = plan.model_dump(mode="json")
    monkeypatch.setattr(trip_service_module, "analyze_trip_edit", analyze)
    service = TripService(session)
    service._user_repository = FakeUserRepository()
    service._trip_repository = repository

    async def edit():
        return await service.edit_trip_plan(
            telegram_id=9000000001,
            trip_id=7,
            instruction=case.steps[0].user_message,
            enrichment_service=enrichment,
        )

    if defect in {"kept_missing", "empty_period"}:
        with pytest.raises(AIServiceError, match="Маршрут не изменён"):
            await edit()
        assert repository.update_arguments is None
        assert session.commit_count == 0
        assert enrichment.called is (defect != "empty_period")
    else:
        await edit()
        saved = repository.update_arguments
        assert len(saved["plan_data"]["days"][1]["afternoon"]) == 1
        assert saved["plan_data"]["days"][1]["afternoon"][0].startswith(
            "Тестовый музей 4:"
        )
        assert saved["preferences_data"]["must_visit_places"] == [
            "Art Aquarium Museum GINZA"
        ]
        assert session.commit_count == 1
    assert repository.trip.plan_data == plan.model_dump(mode="json")
    assert repository.trip.preferences_data == case.preferences.model_dump(mode="json")
