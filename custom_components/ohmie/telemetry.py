"""Telemetry for the edge protocol: one reading per Asset per sample, a batch per Asset every few
minutes, and a queue of unsent batches that survives a restart (edge-protocol.md, "telemetry").

Ohmie's units: soc_pct, power_kw (+ import or charge, - export, discharge or production),
energy_kwh (cumulative, the same sign: a pv counter falls, a meter's is import less export), temp_c,
voltage_v (a controlled circuit's supply: none there is a network cut).
A binding's `options.signs` flips a role whose entity counts the other way.
No Home Assistant imports."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .model import Binding, EntityState

ROLE_FIELD = {"soc": "soc_pct", "power": "power_kw", "energy": "energy_kwh", "temp": "temp_c",
              "voltage": "voltage_v"}
SCALE = {
    "power": {"W": 0.001, "kW": 1.0, "MW": 1000.0},
    "energy": {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0},
    "voltage": {"mV": 0.001, "V": 1.0, "kV": 1000.0},
}
MAX_READINGS = 1000   # per message (edge-protocol.md)
MAX_PENDING = 4000    # unsent batches kept: ~3 days for 4 devices at one batch per 5 min


def _value(role: str, st: EntityState) -> float | None:
    try:
        v = float(st.state)
    except (TypeError, ValueError):
        return None
    unit = st.attributes.get("unit_of_measurement")
    if role in SCALE:
        factor = SCALE[role].get(unit or {"power": "kW", "energy": "kWh", "voltage": "V"}[role])
        return None if factor is None else v * factor
    if role == "temp":
        return {"°F": (v - 32) * 5 / 9, "K": v - 273.15}.get(unit or "°C", v)
    if role == "soc":
        return v if 0 <= v <= 100 else None
    return v


def reading(b: Binding, states: Mapping[str, EntityState], at: datetime) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    signs = b.options.get("signs") or {}
    values: dict[str, float] = {}
    for role in (*ROLE_FIELD, "energy_out"):
        entity = b.entities.get(role)
        st = states.get(entity) if entity else None
        v = _value("energy" if role == "energy_out" else role, st) if st is not None else None
        if v is not None:
            values[role] = v * signs.get(role, 1)
    if b.device_type == "pv":  # production is negative, whatever the sensor's own sign
        values = {r: -abs(v) for r, v in values.items()}
    if "energy_out" in b.entities:  # a meter's net counter: import less export, or none
        imp, exp = values.pop("energy", None), values.pop("energy_out", None)
        if imp is not None and exp is not None:
            values["energy"] = imp - exp
    for role, field in ROLE_FIELD.items():
        if role in values:
            out[field] = round(values[role], 4)
    relay = states.get(b.entities.get("switch", ""))
    if "power_kw" not in out and relay is not None and b.options.get("max_kw") is not None and \
            relay.state in ("on", "off"):
        # A relay with no power sensor: its rated power while on (read only, never switched).
        out["power_kw"] = float(b.options["max_kw"]) if relay.state == "on" else 0.0
    return {"time": at.isoformat(), **out} if out else None


@dataclass(frozen=True)
class Batch:
    asset: str
    msg_id: str           # Nats-Msg-Id: a hash of the readings, so a replay is dropped
    payload: dict[str, Any]


class Batcher:
    def __init__(self) -> None:
        self._readings: dict[str, list[dict[str, Any]]] = {}

    def add(self, asset: str, r: dict[str, Any]) -> None:
        self._readings.setdefault(asset, []).append(r)

    def flush(self, now: datetime) -> list[Batch]:
        out = []
        for asset, rs in self._readings.items():
            for i in range(0, len(rs), MAX_READINGS):
                chunk = rs[i:i + MAX_READINGS]
                digest = hashlib.sha256(json.dumps([asset, chunk], sort_keys=True).encode())
                out.append(Batch(asset, digest.hexdigest()[:32],
                                 {"v": 1, "type": "telemetry", "ts": now.isoformat(),
                                  "readings": chunk}))
        self._readings.clear()
        return out


class Queue:
    """Unsent batches, oldest first. Full: the oldest go (Ohmie keeps 30 days back anyway)."""

    def __init__(self) -> None:
        self._q: deque[Batch] = deque(maxlen=MAX_PENDING)

    def __len__(self) -> int:
        return len(self._q)

    def add(self, b: Batch) -> None:
        self._q.append(b)

    def peek(self) -> Batch | None:
        return self._q[0] if self._q else None

    def done(self, msg_id: str) -> None:
        if self._q and self._q[0].msg_id == msg_id:
            self._q.popleft()

    def to_list(self) -> list[dict[str, Any]]:
        return [{"asset": b.asset, "msg_id": b.msg_id, "payload": b.payload} for b in self._q]

    @classmethod
    def from_list(cls, items: list[dict[str, Any]]) -> Queue:
        q = cls()
        for i in items:
            q.add(Batch(i["asset"], i["msg_id"], i["payload"]))
        return q
