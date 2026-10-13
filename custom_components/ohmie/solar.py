"""The member's solar forecast, from Home Assistant's Energy dashboard: each solar source's forecast
integration (Solcast, Forecast.Solar, ...) through HA's brand-neutral energy platform, which
returns hourly Wh. Ohmie wants 30-minute periods in kW, production negative (the edge
convention)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

HALF = timedelta(minutes=30)


def periods_from_wh_hours(forecasts: list[dict[str, float]],
                          now: datetime) -> list[tuple[str, float]]:
    """The summed forecasts as (period start ISO, kW) from the current hour on. An hour's Wh is its
    mean power, so both of its half hours get Wh / 1000 kW, negated (production)."""
    total: dict[datetime, float] = {}
    for f in forecasts:
        for iso, wh in f.items():
            t = datetime.fromisoformat(iso)
            total[t] = total.get(t, 0.0) + float(wh)
    hour = now.replace(minute=0, second=0, microsecond=0)
    out = []
    for t in sorted(total):
        if t < hour:
            continue
        kw = round(-total[t] / 1000, 4)
        out += [(t.isoformat(), kw), ((t + HALF).isoformat(), kw)]
    return out


async def energy_dashboard_forecasts(hass: HomeAssistant) -> tuple[list[dict[str, float]], str]:
    """Every solar forecast the Energy dashboard knows, and a source label naming their domains."""
    try:
        from homeassistant.components.energy.data import async_get_manager
        from homeassistant.components.energy.websocket_api import async_get_energy_platforms
    except ImportError:
        return [], ""
    manager = await async_get_manager(hass)
    data: dict[str, Any] = dict(manager.data or {})
    entries = {e for src in data.get("energy_sources", []) if src.get("type") == "solar"
               for e in (src.get("config_entry_solar_forecast") or [])}
    platforms = await async_get_energy_platforms(hass)
    out, domains = [], set()
    for entry_id in sorted(entries):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain not in platforms:
            continue
        forecast = await platforms[entry.domain](hass, entry_id)
        if forecast and forecast.get("wh_hours"):
            out.append(dict(forecast["wh_hours"]))
            domains.add(entry.domain)
    return out, "ha-energy:" + "+".join(sorted(domains))
