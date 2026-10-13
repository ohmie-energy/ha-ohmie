"""Ohmie's commands for a member's own automation (api-specs/home-assistant-contract.md, "Control").
Ohmie drives no device: it publishes what to do in shapes that equipment accepts, and the member
wires them, behind their own conditions (blueprints per control shape).

- `command`: charge, discharge, hold, run or idle with a power, for "mode + power" devices. `normal`
  means "use your own mode": Ohmie is not in control (advisory, paused, no consent, the kill switch,
  a reported override, or no current plan).
- `output_on`: the Ohmie switch of an on/off device (a relay, a pump, a charger): on to run or
  charge; in `normal` it shows the member's normal state.
- `next_window`: the next charge (or export) window with the charge level it should reach, for
  "window + target SoC" devices.
No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .edge import PERIOD, Control, Plan, hands_off


@dataclass(frozen=True)
class Command:
    state: str
    power_kw: float = 0.0
    until: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    power_kw: float
    energy_kwh: float
    target_soc_pct: float | None


def command(plan: Plan | None, control: Control | None, asset: str, local_pause: bool,
            paused_until: datetime | None, now: datetime) -> Command:
    if paused_until is not None and now < paused_until:
        return Command("normal", reason="overridden")
    period = plan.period_at(now) if plan else None
    if period is None or not hands_off(plan, control, asset, local_pause):
        return Command("normal", reason="advisory" if period else "no plan")
    return Command(period.action, period.setpoint_kw, period.end)


def capped(cmd: Command, cap_kw: float | None) -> Command:
    """The command under a load-balancing cap (balance.py): a consuming command asks no more than
    the cap, and stops at 0. Export, hold and `normal` are not changed."""
    if cap_kw is None or cmd.state not in ("charge", "run") or cmd.power_kw <= cap_kw:
        return cmd
    if cap_kw <= 0:
        return Command("hold" if cmd.state == "charge" else "idle", 0.0, cmd.until,
                       "load balancing")
    return Command(cmd.state, cap_kw, cmd.until, "load balancing")


def output_on(cmd: Command, normal_state: str) -> bool:
    if cmd.state == "normal":
        return normal_state == "on"
    return cmd.state in ("charge", "run") and cmd.power_kw > 0


def _fraction_left(start: datetime, now: datetime) -> float:
    """How much of the period at `start` is still ahead of `now` (0..1)."""
    return max(0.0, min(1.0, (start + PERIOD - now) / PERIOD))


def next_window(plan: Plan | None, now: datetime, actions: set[str], soc_now: float | None,
                capacity_kwh: float | None) -> Window | None:
    """The window of `actions` we are in, or the next one, and the SoC at its end projected from
    the SoC now through every planned period in between."""
    if plan is None:
        return None
    runs: list[list[int]] = []
    ps = plan.periods
    for i, p in enumerate(ps):
        if p.action not in actions:
            continue
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    run = next((r for r in runs if ps[r[-1]].end > now), None)
    if run is None:
        return None
    kwh = sum(ps[i].setpoint_kw * PERIOD.total_seconds() / 3600 for i in run)
    target = None
    if soc_now is not None and capacity_kwh:
        ahead = sum(p.setpoint_kw * PERIOD.total_seconds() / 3600 * _fraction_left(p.start, now)
                    for p in ps if p.end > now and p.start < ps[run[-1]].end)
        target = round(max(0.0, min(100.0, soc_now + 100 * ahead / capacity_kwh)), 1)
    return Window(ps[run[0]].start, ps[run[-1]].end,
                  round(kwh / (len(run) * PERIOD.total_seconds() / 3600), 3), round(kwh, 3),
                  target)
