"""The fix flow for a supply cut Ohmie suspects (ADR D10): the member says whether the supply to a
controlled-register device was cut. Ohmie learns how often networks cut."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import OhmieApiError


class SupplyCutFlow(RepairsFlow):
    def __init__(self, entry_id: str, event_id: str) -> None:
        self._entry_id, self._event_id = entry_id, event_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is None or not hasattr(entry, "runtime_data"):
                return self.async_abort(reason="not_loaded")
            try:
                await entry.runtime_data.answer_supply(self._event_id, user_input["cut"] == "yes")
            except OhmieApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(data={})
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Required("cut"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=["yes", "no"], translation_key="supply_cut",
                mode=selector.SelectSelectorMode.LIST))}))


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str,
                                data: dict[str, Any] | None) -> RepairsFlow:
    data = data or {}
    return SupplyCutFlow(str(data["entry_id"]), str(data["event_id"]))
