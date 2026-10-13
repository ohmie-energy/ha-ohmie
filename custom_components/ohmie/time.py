"""Ready-by times (the EV, the hot water). Each one changes the member's declaration in Ohmie."""

from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OhmieConfigEntry
from .entity import OhmieEntity


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry,
                            add: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    if rt.role in ("owner", "operator"):
        add([ReadyBy(rt, a) for a in rt.assets if rt.requirement(a)])


class ReadyBy(OhmieEntity, TimeEntity):
    def __init__(self, rt, asset):
        super().__init__(rt, asset, "ready_by")

    @property
    def native_value(self) -> time | None:
        found = self.rt.requirement(self.asset or "")
        at = found[1].get("atLocalTime") if found else None
        return time.fromisoformat(at) if at else None

    async def async_set_value(self, value: time) -> None:
        found = self.rt.requirement(self.asset or "")
        if found is None:
            return

        def change(spec: dict[str, Any]) -> None:
            for r in spec.get("requirements") or []:
                if r.get("assetRef") == self.asset:
                    r["atLocalTime"] = value.strftime("%H:%M")

        await self.rt.update_declaration("ConstraintPolicy", found[0], change)
