"""Перенос узнаёт полные известные названия сохранённого посещения."""

import pytest

from app.services import ai
from scripts import evaluate_routes as evaluation
from tests.test_place_moves import move_analysis

ENGLISH = "Tokyo Station Gallery"
JAPANESE = "東京ステーションギャラリー"
DISPLAY = f"{ENGLISH} ({JAPANESE})"
INSTRUCTION = (
    f"Перенеси {ENGLISH} со второго дня днём на первый день днём. "
    "Все остальные активности оставь без изменений."
)


@pytest.mark.parametrize("returned_name", [ENGLISH, JAPANESE, DISPLAY])
@pytest.mark.parametrize("explicit_name", [ENGLISH, JAPANESE, DISPLAY])
def test_move_accepts_known_saved_alias(returned_name, explicit_name):
    suite = evaluation.load_edit_suite()
    case = suite.cases[5]
    plan = suite.plans[case.current_plan].model_copy(deep=True)
    plan.days[1].afternoon = [f"{DISPLAY}: посетить музей."]
    parsed = move_analysis()
    parsed.place_moves[0].place_name = returned_name
    ai._validate_trip_edit_analysis(
        analysis=parsed,
        current_preferences=case.preferences,
        current_plan=plan,
        instruction=INSTRUCTION.replace(ENGLISH, explicit_name),
    )


@pytest.mark.parametrize("name", ["Tokyo", "Gallery", "другой музей", "Archive Four"])
def test_move_rejects_missing_complete_saved_name(name):
    suite = evaluation.load_edit_suite()
    case = suite.cases[5]
    plan = suite.plans[case.current_plan].model_copy(deep=True)
    plan.days[1].afternoon = [f"{DISPLAY}: посетить музей."]
    parsed = move_analysis()
    parsed.place_moves[0].place_name = DISPLAY
    with pytest.raises(ValueError, match="explicitly named"):
        ai._validate_trip_edit_analysis(
            analysis=parsed,
            current_preferences=case.preferences,
            current_plan=plan,
            instruction=INSTRUCTION.replace(ENGLISH, name),
        )


@pytest.mark.parametrize(
    ("saved", "instruction", "expected"),
    [
        ("Musée Étoile (星美術館)", "Перенеси Musee Etoile на утро", True),
        (f"{ENGLISH}（{JAPANESE}）", INSTRUCTION, True),
        ("Local Museum (Original (Annex))", "Перенеси Original (Annex)", True),
        ("Local Museum (Original (Annex))", "Перенеси Annex", False),
        ("Museum (ABC)", "Перенеси ABC", False),
        (DISPLAY, "Перенеси Tokyo", False),
        (DISPLAY, "", False),
    ],
)
def test_explicitness_uses_only_complete_saved_components(saved, instruction, expected):
    from app.services.place_matching import saved_place_name_is_explicit

    assert (
        saved_place_name_is_explicit(instruction=instruction, saved_name=saved)
        is expected
    )


@pytest.mark.parametrize(
    "defect", ["partial", "unknown", "ambiguous", "wrong_day", "wrong_id"]
)
def test_independent_move_oracle_rejects_invalid_alias_or_scope(defect):
    suite = evaluation.load_edit_suite()
    case = suite.cases[6]
    context = suite.contexts[case.context].model_copy(deep=True)
    expected = case.steps[0].expectations.moved_visits
    parsed = move_analysis()
    request = parsed.place_moves[0]
    request.place_name = DISPLAY
    if defect == "partial":
        request.place_name = "Tokyo"
    elif defect == "unknown":
        request.place_name = "Unknown Gallery"
    elif defect == "ambiguous":
        another = context.places[-1].model_copy(deep=True)
        another.source_place_id = "duplicate-name"
        another.name = DISPLAY
        context.places.append(another)
    elif defect == "wrong_day":
        request.target_day = 2
    else:
        expected = [
            item.model_copy(update={"moved_place_id": "eval-edit-aida"})
            for item in expected
        ]
    assert not evaluation.assess_move_requests([request], expected, context)
