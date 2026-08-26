from typing import Any

import pytest

from app.bot.services.trip_formatter import format_trip_plan


def _make_trip_plan(
    *,
    duration_days: int = 1,
    title: str = "Исторический центр",
) -> dict[str, Any]:
    return {
        "destination": "Стамбул",
        "duration_days": duration_days,
        "summary": "Тестовый маршрут.",
        "days": [
            {
                "day": 1,
                "title": title,
                "morning": [],
                "afternoon": [],
                "evening": [],
            }
        ],
        "practical_tips": [],
    }


@pytest.mark.parametrize(
    ("duration_days", "expected_text"),
    [
        (1, "📅 Продолжительность: 1 день"),
        (2, "📅 Продолжительность: 2 дня"),
        (4, "📅 Продолжительность: 4 дня"),
        (5, "📅 Продолжительность: 5 дней"),
        (11, "📅 Продолжительность: 11 дней"),
        (21, "📅 Продолжительность: 21 день"),
    ],
)
def test_format_trip_plan_uses_correct_duration_word(
    duration_days: int,
    expected_text: str,
) -> None:
    result = format_trip_plan(
        _make_trip_plan(
            duration_days=duration_days,
        )
    )

    assert expected_text in result


def test_format_trip_plan_removes_duplicate_day_prefix() -> None:
    result = format_trip_plan(
        _make_trip_plan(
            title="День 1 – Султанахмет",
        )
    )

    assert "📍 День 1: Султанахмет" in result
    assert "День 1: День 1" not in result


def test_format_trip_plan_handles_title_containing_only_day_number() -> None:
    result = format_trip_plan(
        _make_trip_plan(
            title="День 1",
        )
    )

    assert "📍 День 1\n" in result
    assert "📍 День 1:" not in result


def test_format_trip_plan_keeps_regular_day_title() -> None:
    result = format_trip_plan(
        _make_trip_plan(
            title="Исторический центр",
        )
    )

    assert "📍 День 1: Исторический центр" in result
