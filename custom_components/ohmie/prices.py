"""The cheap sensors over labelled periods. On a ToU plan the plan's own windows say what is cheap
(GET /v1/members/<id>/sites/<site>/price: cheap at its lowest rate); otherwise Ohmie's market label
on the nodal forecast (p50 at or below the node's lowest realised quartile of 28 days). No Home
Assistant imports."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .edge import PERIOD


def _rows(forecast: list[dict[str, Any]]) -> list[tuple[datetime, dict[str, Any]]]:
    return sorted(((datetime.fromisoformat(r["start"].replace("Z", "+00:00")), r)
                   for r in forecast), key=lambda x: x[0])


def period_at(forecast: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    return next((r for s, r in _rows(forecast) if s <= now < s + PERIOD), None)


def price_at(forecast: list[dict[str, Any]], now: datetime) -> float | None:
    r = period_at(forecast, now)
    return None if r is None or r.get("price_p50") is None else float(r["price_p50"])


def cheap_now(forecast: list[dict[str, Any]], now: datetime) -> bool | None:
    r = period_at(forecast, now)
    return None if r is None or r.get("label") is None else r["label"] == "cheap"


def next_cheap_window(forecast: list[dict[str, Any]],
                      now: datetime) -> tuple[datetime, datetime] | None:
    """The cheap window we are in, or the next one: (start, end)."""
    runs: list[tuple[datetime, datetime]] = []
    for s, r in _rows(forecast):
        if r.get("label") != "cheap":
            continue
        if runs and runs[-1][1] == s:
            runs[-1] = (runs[-1][0], s + PERIOD)
        else:
            runs.append((s, s + PERIOD))
    return next((w for w in runs if w[1] > now), None)
