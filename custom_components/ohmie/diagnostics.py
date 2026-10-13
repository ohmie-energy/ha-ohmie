"""Diagnostics: the runtime state with the token, the ICP and the Site's location left out."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import OhmieConfigEntry

REDACT = {"token", "access_token", "refresh_token", "id_token", "icp", "latitude", "longitude"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant,
                                             entry: OhmieConfigEntry) -> dict[str, Any]:
    rt = entry.runtime_data
    now = dt_util.utcnow()
    return {
        "entry": async_redact_data(dict(entry.data), REDACT),
        "transport": {"nats": rt.link.connected, "rest_fallback": rt.rest_mode,
                      "unsent_batches": len(rt.tel_queue), "unsent_acks": len(rt.ack_queue)},
        "paused_here": rt.local_pause,
        "control": None if rt.control is None else {
            "kill_switch": rt.control.kill_switch,
            "consent": {a: {"granted": c.granted, "policy_version": c.policy_version}
                        for a, c in rt.control.consent.items()}},
        "assets": {a: {"device_type": t, "mode": rt.mode(a),
                       "plan": None if (p := rt.plans.get(a)) is None else {
                           "schedule_uuid": p.schedule_uuid,
                           "computed_at": p.computed_at.isoformat(),
                           "effective_delivery": p.effective_delivery,
                           "now": (q.action if (q := p.period_at(now)) else None)},
                       "command": rt.command(a).state}
                   for a, t in rt.assets.items()},
    }
