"""Sensors: per device the plan's action, setpoint, reason and mode; for the Site the price now and
the next cheap window."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OhmieConfigEntry
from .edge import Period
from .entity import OhmieEntity
from .prices import next_cheap_window, period_at, price_at

ACTIONS = ["charge", "hold", "discharge", "run", "idle"]
COMMANDS = [*ACTIONS, "normal"]


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry,
                            add: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    entities: list[SensorEntity] = [PriceNow(rt), NextCheapWindow(rt), LeftOnTable(rt)]
    entities += [Headroom(rt, phase) for phase in sorted(rt.phase_entities)]
    for asset, device_type in rt.assets.items():
        if device_type in ("pv", "meter"):
            continue  # nothing is planned for them
        entities += [Action(rt, asset), Setpoint(rt, asset), Reason(rt, asset), Mode(rt, asset),
                     CommandSensor(rt, asset), NextWindow(rt, asset, "next_charge")]
        if device_type == "battery":
            entities += [NextWindow(rt, asset, "next_export"), Reserve(rt, asset)]
    add(entities)


class _PlanSensor(OhmieEntity, SensorEntity):
    def _period(self) -> Period | None:
        plan = self.rt.plans.get(self.asset or "")
        return plan.period_at(dt_util.utcnow()) if plan else None


class Action(_PlanSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ACTIONS

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "action")

    @property
    def native_value(self) -> str | None:
        p = self._period()
        return p.action if p else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.rt.plans.get(self.asset or "")
        nxt = plan.next_change(dt_util.utcnow()) if plan else None
        return {"next_change": nxt.isoformat() if nxt else None,
                "schedule_uuid": plan.schedule_uuid if plan else None}


class Setpoint(_PlanSensor):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "setpoint")

    @property
    def native_value(self) -> float | None:
        p = self._period()
        return p.setpoint_kw if p else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.rt.plans.get(self.asset or "")
        now = dt_util.utcnow()
        return {"plan": [{"start": p.start.isoformat(), "setpoint_kw": p.setpoint_kw,
                          "action": p.action} for p in (plan.periods if plan else ())
                         if p.end > now]}


class Reason(_PlanSensor):
    def __init__(self, rt, asset):
        super().__init__(rt, asset, "reason")

    @property
    def native_value(self) -> str | None:
        p = self._period()
        return (p.reason or "")[:255] if p else None


class Mode(OhmieEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["advisory", "hands-off"]

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "mode")

    @property
    def native_value(self) -> str:
        return self.rt.mode(self.asset or "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.rt.plans.get(self.asset or "")
        return {"control_reason": plan.control_reason if plan else None,
                "paused": self.rt.paused}


class PriceNow(OhmieEntity, SensorEntity):
    _attr_native_unit_of_measurement = "NZD/kWh"
    _attr_suggested_display_precision = 3

    def __init__(self, rt):
        super().__init__(rt, None, "price_now")

    def _signal(self) -> dict[str, Any] | None:
        return period_at(self.rt.signal, dt_util.utcnow())

    @property
    def native_value(self) -> float | None:
        """What the Site pays now (its plan, else its ICP's total), else the nodal forecast."""
        t = self._signal()
        if t is not None and t.get("import_price") is not None:
            return _kwh(t["import_price"])
        return _kwh(price_at(self.rt.forecast, dt_util.utcnow()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        r = period_at(self.rt.forecast, dt_util.utcnow()) or {}
        t = self._signal() or {}
        basis = t.get("import_basis")
        out = {"node": self.rt.node, "market_label": r.get("label"),
               "market_p50": _kwh(r.get("price_p50")), "p10": _kwh(r.get("price_p10")),
               "p90": _kwh(r.get("price_p90")),
               "basis": ("your plan, ex GST" if basis in ("spot", "schedule", "flat") else
                         "total: nodal x losses + lines, ex GST" if basis == "total" else
                         "nodal forecast p50, ex GST (add your power plan or ICP)")}
        if t:
            out.update(plan=basis if basis != "total" else None, window=t.get("import_window"),
                       label=t.get("label"), export=_kwh(t.get("export_price")),
                       controlled=_kwh(t.get("controlled_price")),
                       lines_known=t.get("lines_known"), nodal=_kwh(t.get("nodal_price")),
                       loss_factor=t.get("loss_factor"), lines=_kwh(t.get("import_lines")))
        return out


def _kwh(v: float | None) -> float | None:
    return None if v is None else round(v / 1000, 5)


class NextCheapWindow(OhmieEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, rt):
        super().__init__(rt, None, "next_cheap_window")

    def _window(self) -> tuple[datetime, datetime] | None:
        return next_cheap_window(self.rt.labelled, dt_util.utcnow())

    @property
    def native_value(self) -> datetime | None:
        w = self._window()
        return w[0] if w else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._window()
        return {"end": w[1].isoformat() if w else None}


class CommandSensor(OhmieEntity, SensorEntity):
    """What a member's automation should do now: the "mode + power" control shape. `normal` = use
    the device's own mode (Ohmie is not in control)."""
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = COMMANDS

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "command")

    @property
    def native_value(self) -> str:
        return self.rt.command(self.asset or "").state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.rt.command(self.asset or "")
        return {"power_kw": c.power_kw, "power_w": round(c.power_kw * 1000),
                "power_magnitude_w": abs(round(c.power_kw * 1000)),
                "until": c.until.isoformat() if c.until else None, "reason": c.reason}


class NextWindow(OhmieEntity, SensorEntity):
    """The "window + target SoC" control shape: the charge (or export) window we are in, or the
    next one, and the charge level the device should reach by its end."""
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    ACTIONS = {"next_charge": {"charge", "run"}, "next_export": {"discharge"}}

    def __init__(self, rt, asset, key):
        super().__init__(rt, asset, key)
        self._actions = self.ACTIONS[key]

    @property
    def native_value(self) -> datetime | None:
        w = self.rt.window(self.asset or "", self._actions)
        return w.start if w else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self.rt.window(self.asset or "", self._actions)
        return {"end": w.end.isoformat() if w else None,
                "power_kw": w.power_kw if w else None,
                "energy_kwh": w.energy_kwh if w else None,
                "target_soc_pct": w.target_soc_pct if w else None,
                "commanded": self.rt.mode(self.asset or "") == "hands-off"}


class Reserve(OhmieEntity, SensorEntity):
    """The battery's reserve that Ohmie plans around: for "reserve" settings on an inverter."""
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, rt, asset):
        super().__init__(rt, asset, "reserve")

    @property
    def native_value(self) -> float | None:
        return self.rt.reserve(self.asset or "")


class LeftOnTable(OhmieEntity, SensorEntity):
    """Last complete week: what following Ohmie's plans would have saved, NZD incl GST (ADR D10).
    Attributes: what was paid on the plan, truthful spot, Ohmie's plan, and this week so far."""
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "NZD"
    _attr_suggested_display_precision = 2

    def __init__(self, rt):
        super().__init__(rt, None, "left_on_table")

    def _weeks(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(the newest complete week, the current week)."""
        weeks = (self.rt.weekly_report or {}).get("weeks") or []
        today = dt_util.now().date()
        monday = (today - timedelta(days=today.weekday())).isoformat()
        done = [w for w in weeks if w["week_start"] < monday]
        now = next((w for w in weeks if w["week_start"] == monday), None)
        return (done[-1] if done else None), now

    @property
    def native_value(self) -> float | None:
        week, _ = self._weeks()
        return _incl(week, "left_on_table")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        week, now = self._weeks()
        out: dict[str, Any] = {"this_week_so_far": _incl(now, "left_on_table"),
                               "notes": (self.rt.weekly_report or {}).get("notes") or []}
        if week:
            out.update(week_start=week["week_start"], paid=_incl(week, "paid"),
                       daily_charges=_incl(week, "daily"), spot=_incl(week, "spot"),
                       ohmie=_incl(week, "ohmie"), import_kwh=week["import_kwh"],
                       export_kwh=week["export_kwh"],
                       coverage=round(week["covered"] / week["periods"], 3)
                       if week["periods"] else None)
        return out


def _incl(week: dict[str, Any] | None, key: str) -> float | None:
    v = (week or {}).get(key)
    return None if v is None else v["incl_gst"]


class Headroom(OhmieEntity, SensorEntity):
    """Amps left on a phase to its limit, from the member's own reading (ADR D11)."""
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, rt, phase: str):
        super().__init__(rt, None, f"headroom_{phase.lower()}")
        self.phase = phase

    @property
    def native_value(self) -> float | None:
        return self.rt.headroom().get(self.phase)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        b = self.rt.budget
        if b is None:
            return {"budget": "none yet"}
        return {"limit_a": b.import_a.get(self.phase), "load_a":
                None if (v := self.rt.phase_loads().get(self.phase)) is None else round(v, 1),
                "limit_pct": b.limit_pct, "watch_pct": b.watch_pct}

