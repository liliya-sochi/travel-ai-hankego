import pytest

from app.services.opening_hours import (
    format_opening_hours,
    infer_available_periods,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Mo-Su 09:00-18:30",
            "пн–вс: 09:00–18:30",
        ),
        (
            "Tu-Su 09:00-19:00",
            "вт–вс: 09:00–19:00",
        ),
        (
            "Mo, We-Su 09:00-18:00",
            "пн, ср–вс: 09:00–18:00",
        ),
        (
            "Mo,PH off; Tu-Su 09:30-17:00",
            ("пн, праздничные дни: закрыто; вт–вс: 09:30–17:00"),
        ),
        (
            "09:00-21:00",
            "09:00–21:00",
        ),
        (
            "24/7",
            "круглосуточно",
        ),
    ],
)
def test_format_opening_hours_translates_simple_osm_syntax(
    source: str,
    expected: str,
) -> None:
    assert format_opening_hours(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "sunrise-sunset",
        "Mo-Fr sunrise-sunset",
        "week 01-10",
    ],
)
def test_format_opening_hours_keeps_unknown_syntax(
    source: str,
) -> None:
    assert format_opening_hours(source) == source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Mo-Su 09:00-18:00",
            ("morning", "afternoon"),
        ),
        (
            "Mo-Su 09:00-18:30",
            ("morning", "afternoon"),
        ),
        (
            "Mo-Su 18:00-19:00",
            ("evening",),
        ),
        (
            "Mo-Su 12:00-17:30",
            ("afternoon",),
        ),
        (
            "Mo-Su 18:00-23:00",
            ("evening",),
        ),
        (
            "09:00-21:00",
            ("morning", "afternoon", "evening"),
        ),
        (
            "22:00-08:00",
            ("morning", "evening"),
        ),
        (
            "Mo off; Tu-Su 09:30-17:00",
            ("morning", "afternoon"),
        ),
        (
            "We-Su 9:00-17:00",
            ("morning", "afternoon"),
        ),
        (
            "Tu off; Jan 1 off; May 1 off; Dec 25 off; Tu-Su 09:30-18:15",
            ("morning", "afternoon"),
        ),
        (
            "Mo-Su 09:30-19:30; Dec 24,31 09:30-14:00; "
            "Jan 01 off; May 01 off; Dec 25 off",
            ("morning", "afternoon", "evening"),
        ),
        (
            "Tu-Fr, Su 11:00-19:00; Sa 11:00-22:00; May 1 off; Dec 25 off",
            ("morning", "afternoon", "evening"),
        ),
        (
            "Mo-Su,PH 10:00-19:00; Dec 31 10:00-17:30; Jan 01 off",
            ("morning", "afternoon", "evening"),
        ),
        (
            "Sa-Su, Tu-Th 10:00-18:00; Fr,We[2] 10:00-20:00; Mo off",
            ("morning", "afternoon", "evening"),
        ),
        (
            "Mo-Su off",
            (),
        ),
        (
            "24/7",
            ("morning", "afternoon", "evening"),
        ),
    ],
)
def test_infer_available_periods_from_simple_osm_syntax(
    source: str,
    expected: tuple[str, ...],
) -> None:
    assert infer_available_periods(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        None,
        "",
        "sunrise-sunset",
        "Mo-Fr sunrise-sunset",
        "Mo-Su 25:00-26:00",
        "Mo-Su 109:00-17:00",
        "Mo-Su 09:00-09:00",
    ],
)
def test_infer_available_periods_keeps_unknown_schedule_unrestricted(
    source: str | None,
) -> None:
    assert infer_available_periods(source) is None
