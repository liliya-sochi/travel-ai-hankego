import pytest

from app.services.opening_hours import format_opening_hours


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
