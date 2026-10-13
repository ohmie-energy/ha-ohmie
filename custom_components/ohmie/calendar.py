"""The plan calendar: every planned charge and run window of the Site's devices."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OhmieConfigEntry
from .entity import OhmieEntity


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry,
                            add: AddConfigEntryEntitiesCallback) -> None:
    add([PlanCalendar(entry.runtime_data)])


class PlanCalendar(OhmieEntity, CalendarEntity):
    def __init__(self, rt):
        super().__init__(rt, None, "plan")

    def _events(self) -> list[CalendarEvent]:
        out = []
        for asset, plan in sorted(self.rt.plans.items()):
            for start, end, action in plan.windows():
                out.append(CalendarEvent(start=start, end=end, summary=f"{asset}: {action}",
                                         uid=f"{plan.schedule_uuid}-{start.isoformat()}"))
        return sorted(out, key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        return next((e for e in self._events() if e.end > now), None)

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime,
                               end_date: datetime) -> list[CalendarEvent]:
        return [e for e in self._events() if e.end > start_date and e.start < end_date]
