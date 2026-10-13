"""Load balancing at the edge (ADR D11, Phase 6.7e). Ohmie's backend publishes a budget per Site
(per-phase limits, the devices that give way in order); this engine keeps to it from the member's
own phase readings, every few seconds, offline too. It lowers only Ohmie's own commands (caps in
kW per device), so the member's blueprints follow, and it is not protection: the breaker, the fuse
and the charger's own load management stay that. No Home Assistant imports.

One action at a time: the engine changes one device, then waits SETTLE (three readings at 3 s: a
charger ramps, a relay switches, the meter catches up) and judges the result before it acts again.

- Above limit_pct of a phase's limit (the member's commit): first a device that can slow down (an EV
  charger, a battery) goes down, at most to its minimum. Only when none can slow further does one
  device stop: the smallest that clears the overload, else the largest. Switching big loads on and
  off is the last resort.
- Below watch_pct on every phase of a device: the power comes back, one device per step: a device
  that modulates by 10 % of its power, a relay only when all of it (and 10 %) fits.
- In between, the caps hold, so it does not flap.

Limits: each phase of the main connection; each circuit with its own reading (only the devices on
it, or on a circuit fed from it, give way); and the voltage of a phase: a sag of sag_pct below its
reference (the highest reading of the last REFERENCE_FOR) or below floor_v counts as an overload.
A sag only slows (or stops) devices that modulate: a relay is not the cause.
Every action is a Decision with its reason in plain words, for the Logbook and Ohmie's history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

STEP = 0.10                         # of the device's power, per step down on a sag or back up
SETTLE = timedelta(seconds=9)       # three readings at 3 s before the next action
RELAY_MARGIN = 1.10                 # a relay comes back only when its power and 10 % fit
REFERENCE_FOR = timedelta(minutes=10)


@dataclass(frozen=True)
class Device:
    asset: str
    phases: tuple[str, ...]
    max_kw: float
    min_kw: float
    modulates: bool
    circuit: str | None = None


@dataclass(frozen=True)
class Circuit:
    name: str
    parent: str | None
    phases: tuple[str, ...]
    limit_a: float


@dataclass(frozen=True)
class Budget:
    site: str
    volts: float
    limit_pct: float
    watch_pct: float
    import_a: Mapping[str, float]
    devices: tuple[Device, ...]  # in the order they give way
    circuits: tuple[Circuit, ...] = ()
    sag_pct: float = 10.0
    floor_v: float = 216.2


def parse_budget(msg: Mapping[str, Any]) -> Budget:
    devices = sorted(msg.get("devices") or [], key=lambda d: (d["priority"], d["asset"]))
    volt = msg.get("voltage") or {}
    return Budget(site=msg["site"], volts=float(msg["volts"]), limit_pct=float(msg["limit_pct"]),
                  watch_pct=float(msg["watch_pct"]),
                  import_a={p: float(v["import_a"]) for p, v in msg["phases"].items()},
                  devices=tuple(Device(d["asset"], tuple(d["phases"]), float(d["max_kw"]),
                                       float(d["min_kw"]), bool(d["modulates"]), d.get("circuit"))
                                for d in devices),
                  circuits=tuple(Circuit(c["name"], c.get("parent"), tuple(c["phases"]),
                                         float(c["limit_a"])) for c in msg.get("circuits") or []),
                  sag_pct=float(volt.get("sag_pct", 10.0)),
                  floor_v=float(volt.get("floor_v", 216.2)))


def headroom(budget: Budget, load_a: Mapping[str, float]) -> dict[str, float]:
    """Amps left on each phase with a reading, to its limit."""
    return {p: round(budget.import_a[p] - a, 1) for p, a in load_a.items() if p in budget.import_a}


@dataclass(frozen=True)
class Decision:
    asset: str
    outcome: str            # limited | restored
    cap_kw: float | None    # the new cap; None: back to Ohmie's plan
    text: str               # why, in plain words


@dataclass(frozen=True)
class _Zone:
    """One limit and what it reads: a phase of the main connection, or a circuit."""
    label: str
    load_a: float
    limit_a: float
    members: frozenset[str]
    phase: str | None       # the phase, for the main connection; None for a circuit


def _fmt(kw: float) -> str:
    return f"{kw:.1f}"


@dataclass
class Balancer:
    """The engine's state: the caps it holds (kW per device; none = as Ohmie planned), when it may
    act again, and each phase's reference voltage."""
    budget: Budget | None = None
    caps: dict[str, float] = field(default_factory=dict)
    waiting_until: datetime | None = None
    last: str | None = None
    v_ref: dict[str, tuple[float, datetime]] = field(default_factory=dict)
    v_now: dict[str, float] = field(default_factory=dict)

    def step(self, phase_a: Mapping[str, float], wanted: Mapping[str, float], now: datetime,
             circuit_a: Mapping[str, float] | None = None,
             voltage: Mapping[str, float] | None = None) -> Decision | None:
        """At most one action; None when it waits or nothing needs doing. `wanted` is what Ohmie's
        plan asks of each device now (kW, 0 when it is not consuming)."""
        for asset in [a for a, cap in self.caps.items() if wanted.get(a, 0.0) <= cap]:
            del self.caps[asset]  # the plan asks no more than the cap: nothing to hold back
        self.v_now = dict(voltage or {})
        sags = self._sags(self.v_now, now)
        if self.budget is None or (self.waiting_until is not None and now < self.waiting_until):
            return None
        zones = self._zones(phase_a, circuit_a or {})
        decision = (self._shed(zones, wanted) or self._shed_sag(sags, wanted)
                    or self._release(zones, sags, wanted))
        self.waiting_until = now + SETTLE if decision else None
        if decision:
            self.last = decision.text
        return decision

    # --- what is over ---

    def _zones(self, phase_a: Mapping[str, float], circuit_a: Mapping[str, float]) -> list[_Zone]:
        b = self.budget
        assert b is not None
        zones = [_Zone(p, a, b.import_a[p],
                       frozenset(d.asset for d in b.devices if p in d.phases), p)
                 for p, a in phase_a.items() if p in b.import_a]
        for c in b.circuits:
            if c.name not in circuit_a:
                continue
            under = {c.name} | {x.name for x in b.circuits if x.parent == c.name}
            zones.append(_Zone(c.name, circuit_a[c.name], c.limit_a,
                               frozenset(d.asset for d in b.devices if d.circuit in under), None))
        return zones

    def _sags(self, voltage: Mapping[str, float], now: datetime) -> dict[str, str]:
        """Phases whose voltage sags, with why; the reference is updated first."""
        b = self.budget
        out = {}
        for p, v in voltage.items():
            ref = self.v_ref.get(p)
            if ref is None or v >= ref[0] or now - ref[1] > REFERENCE_FOR:
                self.v_ref[p] = ref = (v, now)
            if b is None:
                continue
            drop = (ref[0] - v) / ref[0] * 100
            if drop > b.sag_pct:
                out[p] = f"{p} at {v:.0f} V, {drop:.0f} % below {ref[0]:.0f} V"
            elif v < b.floor_v:
                out[p] = f"{p} at {v:.0f} V, below {b.floor_v:g} V"
        return out

    def _sag_pct(self, phase: str, voltage_now: float | None) -> float:
        ref = self.v_ref.get(phase)
        if ref is None or voltage_now is None:
            return 0.0
        return (ref[0] - voltage_now) / ref[0] * 100

    # --- giving way ---

    def _asked(self, d: Device, wanted: Mapping[str, float]) -> float:
        return self.caps.get(d.asset, wanted.get(d.asset, 0.0))

    def _shed(self, zones: list[_Zone], wanted: Mapping[str, float]) -> Decision | None:
        b = self.budget
        assert b is not None
        kw_per_a = b.volts / 1000
        over = [(z, (z.load_a - z.limit_a * b.limit_pct / 100) * kw_per_a) for z in zones]
        over = [(z, kw) for z, kw in over if kw > 0]
        if not over:
            return None
        zone, need = max(over, key=lambda o: o[1])
        ceiling = zone.limit_a * b.limit_pct / 100
        why = (f"{zone.label} at {zone.load_a:.1f} A, over {ceiling:.1f} A "
               f"({b.limit_pct:g} % of {zone.limit_a:g} A)")
        on = [d for d in b.devices if d.asset in zone.members and self._asked(d, wanted) > 0]
        for d in on:  # first: slow down
            asked = self._asked(d, wanted)
            if d.modulates and asked > d.min_kw:
                new = round(max(d.min_kw, asked - need * len(d.phases)), 3)
                return self._cap(d, new, f"{why}: {d.asset} slowed from {_fmt(asked)} to "
                                         f"{_fmt(new)} kW")
        if not on:
            return None  # nothing of Ohmie's draws on it
        share = {d.asset: self._asked(d, wanted) / len(d.phases) for d in on}
        clears = [d for d in on if share[d.asset] >= need]
        stop = (min(clears, key=lambda d: share[d.asset]) if clears
                else max(on, key=lambda d: share[d.asset]))
        return self._cap(stop, 0.0, f"{why}: {stop.asset} "
                                    f"{'stopped' if stop.modulates else 'switched off'}")

    def _shed_sag(self, sags: dict[str, str], wanted: Mapping[str, float]) -> Decision | None:
        """A sag: the first device that modulates on the phase steps down (at its minimum, it
        stops). A relay is not moved."""
        b = self.budget
        assert b is not None
        for p, why in sags.items():
            for d in b.devices:
                asked = self._asked(d, wanted)
                if p not in d.phases or not d.modulates or asked <= 0:
                    continue
                new = round(max(d.min_kw, asked - STEP * d.max_kw), 3) if asked > d.min_kw else 0.0
                verb = f"slowed from {_fmt(asked)} to {_fmt(new)} kW" if new else "stopped"
                return self._cap(d, new, f"{why}: {d.asset} {verb}")
        return None

    def _cap(self, d: Device, new: float, text: str) -> Decision:
        self.caps[d.asset] = new
        return Decision(d.asset, "limited", new, text)

    # --- power back ---

    def _release(self, zones: list[_Zone], sags: dict[str, str],
                 wanted: Mapping[str, float]) -> Decision | None:
        b = self.budget
        assert b is not None
        kw_per_a = b.volts / 1000
        main = {z.phase: z for z in zones if z.phase is not None}
        held = [d for d in b.devices if d.asset in self.caps]
        for d in sorted(held, key=lambda d: not d.modulates):  # modulating devices first
            if not all(p in main for p in d.phases):
                continue  # a phase without a reading: nothing comes back on it
            if any(p in sags or self._sag_pct(p, self.v_now.get(p)) > b.sag_pct / 2
                   for p in d.phases):
                continue  # the voltage has not recovered
            mine = [z for z in zones if d.asset in z.members]
            # kW the device may add: a zone's room per phase, times the phases the device spreads on
            room = min((z.limit_a * b.watch_pct / 100 - z.load_a) * kw_per_a * len(d.phases)
                       for z in mine)
            cap, want = self.caps[d.asset], wanted.get(d.asset, 0.0)
            if d.modulates:
                new = cap + min(want - cap, room, STEP * d.max_kw)
                if new < d.min_kw:
                    if room < d.min_kw - cap:
                        continue
                    new = d.min_kw
                if new <= cap:
                    continue
            elif room >= (want - cap) * RELAY_MARGIN:
                new = want
            else:
                continue
            under = ", ".join(f"{z.label} at {z.load_a:.1f} A" for z in mine)
            why = f"{under}, under {b.watch_pct:g} %"
            if new >= want:
                del self.caps[d.asset]
                verb = "back on" if not d.modulates else f"back to its plan, {_fmt(want)} kW"
                return Decision(d.asset, "restored", None, f"{why}: {d.asset} {verb}")
            self.caps[d.asset] = round(new, 3)
            return Decision(d.asset, "restored", round(new, 3),
                            f"{why}: {d.asset} raised from {_fmt(cap)} to {_fmt(new)} kW")
        return None

