"""Home Assistant's Logbook: each load-balancing action with its reason (ADR D11), so a member can
read the night back without asking anyone."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DECISION_EVENT, DOMAIN


@callback
def async_describe_events(hass: HomeAssistant,
                          async_describe_event: Callable[[str, str, Callable], None]) -> None:
    @callback
    def describe(event: Event) -> dict[str, Any]:
        return {"name": "Ohmie", "message": event.data.get("text", "")}

    async_describe_event(DOMAIN, DECISION_EVENT, describe)
