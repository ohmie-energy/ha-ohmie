"""The price sensors: the price now, cheap now, and the next cheap window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ohmie.prices import cheap_now, next_cheap_window, price_at

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
LABELS = ["normal", "dear", "cheap", "cheap", "normal", "dear", "cheap", "normal"]
FORECAST = [{"start": (T0 + timedelta(minutes=30 * i)).isoformat(), "price_p50": 100.0 + i,
             "label": label} for i, label in enumerate(LABELS)]


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_the_price_now_is_the_current_period():
    assert price_at(FORECAST, at(40)) == 101.0
    assert price_at(FORECAST, at(-1)) is None


def test_cheap_now_follows_ohmies_label():
    assert cheap_now(FORECAST, at(65)) is True and cheap_now(FORECAST, at(5)) is False
    assert cheap_now([{**FORECAST[0], "label": None}], at(5)) is None  # no label: unknown


def test_the_next_cheap_window_is_a_run_of_cheap_periods():
    assert next_cheap_window(FORECAST, T0) == (at(60), at(120))
    assert next_cheap_window(FORECAST, at(95)) == (at(60), at(120))  # the one we are in
    assert next_cheap_window(FORECAST, at(125)) == (at(180), at(210))
    assert next_cheap_window(FORECAST, at(215)) is None
    assert next_cheap_window([], T0) is None
