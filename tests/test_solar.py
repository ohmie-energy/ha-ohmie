"""The solar forecast from Home Assistant's Energy dashboard, as Ohmie's 30-minute periods."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.ohmie.solar import periods_from_wh_hours

NOW = datetime(2026, 10, 9, 1, 10, tzinfo=UTC)


def test_hourly_wh_from_every_source_becomes_half_hours_of_production_kw():
    a = {"2026-10-09T01:00:00+00:00": 3000, "2026-10-09T02:00:00+00:00": 1000,
         "2026-10-08T23:00:00+00:00": 500}                 # past: dropped
    b = {"2026-10-09T01:00:00+00:00": 1000}               # a second solar source adds up
    out = periods_from_wh_hours([a, b], NOW)
    assert out == [("2026-10-09T01:00:00+00:00", -4.0), ("2026-10-09T01:30:00+00:00", -4.0),
                   ("2026-10-09T02:00:00+00:00", -1.0), ("2026-10-09T02:30:00+00:00", -1.0)]


def test_no_forecast_is_no_periods():
    assert periods_from_wh_hours([], NOW) == []
