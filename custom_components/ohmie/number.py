"""Targets as numbers. Each one changes the member's declaration in Ohmie (not just this Home
Assistant): the EV's target charge, and the pump's run hours and how often it runs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OhmieConfigEntry
from .entity import OhmieEntity


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry,
                            add: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    entities: list[NumberEntity] = []
    if rt.role in ("owner", "operator"):
        for asset, device_type in rt.assets.items():
            if device_type == "ev" and rt.requirement(asset):
                entities.append(TargetPct(rt, asset))
            if device_type == "flexload" and ("Asset", asset) in rt.decls:
                entities += [RunHours(rt, asset), EveryDays(rt, asset)]
    add(entities)


class TargetPct(OhmieEntity, NumberEntity):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value, _attr_native_max_value, _attr_native_step = 10, 100, 5
    _attr_mode = NumberMode.SLIDER

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "target")

    @property
    def native_value(self) -> float | None:
        found = self.rt.requirement(self.asset or "")
        return found[1].get("valuePct") if found else None

    async def async_set_native_value(self, value: float) -> None:
        found = self.rt.requirement(self.asset or "")
        if found is None:
            return
        policy, _ = found

        def change(spec: dict[str, Any]) -> None:
            for r in spec.get("requirements") or []:
                if r.get("assetRef") == self.asset:
                    r["valuePct"] = value

        await self.rt.update_declaration("ConstraintPolicy", policy, change)


class _Flexload(OhmieEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def _flexload(self) -> dict[str, Any]:
        d = self.rt.decls.get(("Asset", self.asset or ""))
        return (d or {}).get("spec", {}).get("flexload") or {}


class RunHours(_Flexload):
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_native_min_value, _attr_native_max_value, _attr_native_step = 0.5, 24, 0.5

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "run_hours")

    @property
    def native_value(self) -> float | None:
        minutes = self._flexload().get("durationMinutes")
        return None if minutes is None else minutes / 60

    async def async_set_native_value(self, value: float) -> None:
        await self.rt.update_declaration(
            "Asset", self.asset or "",
            lambda spec: spec.setdefault("flexload", {}).update(durationMinutes=round(value * 60)))


class EveryDays(_Flexload):
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_native_min_value, _attr_native_max_value, _attr_native_step = 1, 60, 1

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "every_days")

    @property
    def native_value(self) -> float | None:
        return (self._flexload().get("cycle") or {}).get("everyDays")

    async def async_set_native_value(self, value: float) -> None:
        await self.rt.update_declaration(
            "Asset", self.asset or "",
            lambda spec: spec.setdefault("flexload", {}).setdefault("cycle", {}).update(
                everyDays=int(value)))
