"""Find candidate devices for the wizard: the entity registry matched against the Ohmie catalogue
(GET /v1/catalog), plus the Energy dashboard's solar and grid sensors."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


@dataclass(frozen=True)
class Row:
    entity_id: str
    platform: str
    device_id: str
    device_name: str


@dataclass(frozen=True)
class Candidate:
    id: str
    label: str
    device_id: str
    device_types: tuple[str, ...]
    entities: dict[str, str]          # role -> entity: what Ohmie READS, never drives
    signs: dict[str, int] = field(default_factory=dict)


def _find(rows: list[Row], spec: dict[str, Any]) -> str | None:
    rx = re.compile(spec["pattern"])
    hits = sorted(r.entity_id for r in rows if r.entity_id.split(".")[0] == spec["domain"]
                  and rx.search(r.entity_id.split(".", 1)[1]))
    return hits[0] if hits else None


def match(rows: list[Row], catalog: dict[str, Any]) -> list[Candidate]:
    """One candidate per Home Assistant device that matches a catalogue entry; a device that can be
    several device types (a relay: hot water or a pump) is one candidate."""
    by_device: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        by_device.setdefault((r.platform, r.device_id), []).append(r)
    found: dict[str, Candidate] = {}
    for (platform, device_id), group in sorted(by_device.items()):
        integ = catalog["integrations"].get(platform)
        if not integ:
            continue
        for cand in integ["candidates"]:
            entities = {role: e for role, spec in cand["entities"].items()
                        if (e := _find(group, spec))}
            if not entities:
                continue
            signs = {role: -1 for role, spec in cand["entities"].items()
                     if spec.get("sign") == -1 and role in entities}
            # One relay that can be hot water or a pump is ONE candidate; an inverter that is a
            # battery, solar and a meter is three.
            relay = entities.get("switch")
            cid = f"{platform}:{device_id}:{relay or cand['device_type']}"
            old = found.get(cid)
            if old is not None:
                found[cid] = Candidate(cid, old.label, device_id,
                                       old.device_types + (cand["device_type"],),
                                       {**entities, **old.entities}, {**signs, **old.signs})
                continue
            found[cid] = Candidate(cid, f"{group[0].device_name} ({integ['label']})", device_id,
                                   (cand["device_type"],), entities, signs)
    return list(found.values())


def registry_rows(hass: HomeAssistant) -> list[Row]:
    ents, devs = er.async_get(hass), dr.async_get(hass)
    out = []
    for e in ents.entities.values():
        if e.disabled or e.device_id is None:
            continue
        dev = devs.async_get(e.device_id)
        name = (dev.name_by_user or dev.name) if dev else None
        out.append(Row(e.entity_id, e.platform, e.device_id, name or e.entity_id))
    return out


async def energy_candidates(hass: HomeAssistant) -> list[Candidate]:
    """Solar and grid from the Energy dashboard, as telemetry-only devices (cumulative kWh)."""
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return []
    manager = await async_get_manager(hass)
    return from_energy_prefs(dict(manager.data or {}))


def from_energy_prefs(data: dict[str, Any]) -> list[Candidate]:
    """The Energy dashboard's solar counter, and its grid import and export counters (the meter's
    net counter is import less export, so both are read)."""
    out = []
    for src in data.get("energy_sources", []):
        if src.get("type") == "solar" and src.get("stat_energy_from"):
            out.append(Candidate("energy:solar", "Solar (Energy dashboard)", "", ("pv",),
                                 {"energy": src["stat_energy_from"]}))
        if src.get("type") == "grid":
            flows = src.get("flow_from") or []
            if flows and flows[0].get("stat_energy_from"):
                entities = {"energy": flows[0]["stat_energy_from"]}
                back = src.get("flow_to") or []
                if back and back[0].get("stat_energy_to"):
                    entities["energy_out"] = back[0]["stat_energy_to"]
                out.append(Candidate("energy:grid", "Grid (Energy dashboard)", "", ("meter",),
                                     entities))
    return out
