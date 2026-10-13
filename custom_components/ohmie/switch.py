"""Switches. Per on/off device, the Ohmie switch ("Ohmie · <asset>"): Ohmie's own entity, which
Ohmie turns on and off when the member lets it; the member's automation passes it to the real
device, behind the member's own conditions. Ohmie never switches the real device. Per device,
"Let Ohmie drive" (the member only); for the Site, "Pause Ohmie" (the kill switch: it acts here at
once, also offline, then Ohmie records it)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OhmieConfigEntry
from .entity import OhmieEntity
from .runtime import OUTPUT_TYPES


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry,
                            add: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    entities: list[SwitchEntity] = [Pause(rt)]
    entities += [OhmieSwitch(rt, a) for a in rt.driven if rt.assets[a] in OUTPUT_TYPES]
    if rt.role == "owner":
        entities += [Follow(rt, a) for a in rt.driven]
    add(entities)


class OhmieSwitch(OhmieEntity, SwitchEntity):
    """On to run or charge while Ohmie is in control; the member's normal state otherwise. Set by
    hand, it keeps that until the next half hour (an override)."""

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "output")
        self._attr_name = None          # the device's name: switch.ohmie_<asset>
        self._attr_translation_key = None

    @property
    def is_on(self) -> bool:
        return self.rt.output(self.asset or "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.rt.command(self.asset or "")
        return {"command": c.state, "until": c.until.isoformat() if c.until else None,
                "commanded": c.state != "normal"}

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.rt.set_output(self.asset or "", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.rt.set_output(self.asset or "", False)


class Follow(OhmieEntity, SwitchEntity):
    def __init__(self, rt, asset):
        super().__init__(rt, asset, "follow")

    @property
    def is_on(self) -> bool:
        c = self.rt.control.consent.get(self.asset or "") if self.rt.control else None
        return bool(c and c.granted)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.rt.set_follow(self.asset or "", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.rt.set_follow(self.asset or "", False)


class Pause(OhmieEntity, SwitchEntity):
    def __init__(self, rt):
        super().__init__(rt, None, "pause")

    @property
    def is_on(self) -> bool:
        return self.rt.paused

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"paused_here": self.rt.local_pause,
                "kill_switch": bool(self.rt.control and self.rt.control.kill_switch)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.rt.set_pause(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.rt.set_pause(False)
