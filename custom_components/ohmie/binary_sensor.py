"""Binary sensors: per device "run now"; for the Site "cheap now" and "connected"."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OhmieConfigEntry
from .entity import OhmieEntity
from .prices import cheap_now


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry,
                            add: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    add([CheapNow(rt), Connected(rt)] + [RunNow(rt, a) for a, t in rt.assets.items()
                                         if t not in ("pv", "meter")]
        + ([LoadBalancing(rt)] if rt.phase_entities else []))


class RunNow(OhmieEntity, BinarySensorEntity):
    """On when the plan says charge or run: for simple automations, also when advisory."""

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "run_now")

    @property
    def is_on(self) -> bool | None:
        plan = self.rt.plans.get(self.asset or "")
        p = plan.period_at(dt_util.utcnow()) if plan else None
        return None if p is None else p.runs and p.setpoint_kw > 0


class CheapNow(OhmieEntity, BinarySensorEntity):
    def __init__(self, rt):
        super().__init__(rt, None, "cheap_now")

    @property
    def is_on(self) -> bool | None:
        return cheap_now(self.rt.labelled, dt_util.utcnow())


class Connected(OhmieEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, rt):
        super().__init__(rt, None, "connected")

    @property
    def is_on(self) -> bool:
        return self.rt.connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"transport": "nats" if self.rt.link.connected else
                "rest" if self.rt.rest_mode else "none",
                "unsent_batches": len(self.rt.tel_queue), "unsent_acks": len(self.rt.ack_queue)}


class LoadBalancing(OhmieEntity, BinarySensorEntity):
    """On while load balancing holds a device below Ohmie's plan (ADR D11). Attribute: the caps."""

    def __init__(self, rt):
        super().__init__(rt, None, "load_balancing")

    @property
    def is_on(self) -> bool:
        return bool(self.rt.caps)

    @property
    def extra_state_attributes(self) -> dict:
        return {"caps_kw": dict(self.rt.caps), "last_action": self.rt.balancer.last}

