"""The edge's load balancing (ADR D11). One action at a time: the engine changes one device, waits
SETTLE (three 3 s readings) and judges the result before it does anything else. A device that can
slow down (an EV charger, a battery) gives way first; switching something off is the last resort.
Limits: each phase of the main connection, each circuit with a reading, and a voltage sag.
3-phase Site, 63 A per phase, 230 V. At commit 90 % the ceiling is 56.7 A, the release line
(75 %) 47.25 A."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ohmie.balance import SETTLE, Balancer, parse_budget

T = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
S = timedelta(seconds=3)


def budget(commit=90, release=None, circuits=(), ev_circuit=None):
    return parse_budget({
        "v": 1, "type": "budget", "site": "home", "volts": 230.0, "limit_pct": commit,
        "watch_pct": release or max(commit - 15, 30),
        "phases": {p: {"import_a": 63.0, "export_a": 21.7} for p in ("L1", "L2", "L3")},
        "voltage": {"nominal_v": 230.0, "sag_pct": 10.0, "floor_v": 216.2},
        "circuits": list(circuits),
        "devices": [
            {"asset": "ev", "phases": ["L1", "L2", "L3"], "max_kw": 11.0, "min_kw": 4.14,
             "modulates": True, "priority": 1, "circuit": ev_circuit},
            {"asset": "battery", "phases": ["L1", "L2", "L3"], "max_kw": 6.0, "min_kw": 0.0,
             "modulates": True, "priority": 2, "circuit": None},
            {"asset": "hotwater", "phases": ["L2"], "max_kw": 3.0, "min_kw": 3.0,
             "modulates": False, "priority": 4, "circuit": None}]})


LOW = {"L1": 20.0, "L2": 20.0, "L3": 20.0}
WANT = {"ev": 11.0, "battery": 3.0, "hotwater": 3.0}


def test_one_action_then_it_waits_to_see_the_result():
    b = Balancer(budget())
    # L1 at 63 A: 6.3 A over 56.7 = 1.449 kW on L1; the 3-phase EV gives 3 x 1.449 = 4.347 kW.
    d = b.step({**LOW, "L1": 63.0}, WANT, T)
    assert (d.asset, d.outcome, d.cap_kw) == ("ev", "limited", pytest.approx(6.653, abs=0.001))
    assert d.text == "L1 at 63.0 A, over 56.7 A (90 % of 63 A): ev slowed from 11.0 to 6.7 kW"
    assert b.step({**LOW, "L1": 63.0}, WANT, T + S) is None          # still settling: no guess
    d = b.step({**LOW, "L1": 57.0}, WANT, T + SETTLE)                 # judged: a bit more
    assert d.cap_kw == pytest.approx(6.446, abs=0.001)


def test_a_device_that_slows_down_gives_way_before_a_relay_switches():
    b = Balancer(budget())
    assert b.step({**LOW, "L2": 60.0}, WANT, T).asset == "ev"
    assert "hotwater" not in b.caps


def test_a_relay_goes_off_only_when_nothing_can_slow_down_further():
    b = Balancer(budget())
    b.caps = {"ev": 4.14, "battery": 0.0}
    # 2 kW over on L2: the EV stopping gives only 1.38 kW there; the hot water's 3 kW clears it.
    d = b.step({**LOW, "L2": 65.4}, WANT, T)
    assert (d.asset, d.cap_kw) == ("hotwater", 0.0) and d.text.endswith("hotwater switched off")
    b = Balancer(budget())
    b.caps = {"ev": 4.14, "battery": 0.0}
    assert b.step({**LOW, "L2": 61.05}, WANT, T).asset == "ev"  # the smallest stop that clears


def test_power_comes_back_one_step_at_a_time_below_the_release_line():
    b = Balancer(budget())
    b.caps = {"ev": 6.0, "hotwater": 0.0}
    d = b.step(LOW, WANT, T)
    assert (d.asset, d.outcome, d.cap_kw) == ("ev", "restored", pytest.approx(7.1))
    assert b.step(LOW, WANT, T + S) is None
    assert b.step(LOW, WANT, T + SETTLE).asset == "ev"               # modulators first
    b.caps = {"hotwater": 0.0}
    d = b.step(LOW, WANT, T + 2 * SETTLE)
    assert (d.asset, d.cap_kw) == ("hotwater", None) and b.caps == {}  # back to the plan
    b.caps = {"hotwater": 0.0}
    tight = {"L1": 46.0, "L2": 46.0, "L3": 46.0}                      # 1.25 A of room
    assert b.step(tight, WANT, T + 3 * SETTLE) is None and b.caps == {"hotwater": 0.0}


def test_the_members_commit_and_release_lines():
    assert Balancer(budget(commit=80)).step({**LOW, "L1": 52.0}, WANT, T) is not None  # 50.4 A
    assert Balancer(budget()).step({**LOW, "L1": 52.0}, WANT, T) is None              # 56.7 A
    b = Balancer(budget(commit=80, release=70))                       # 44.1 A
    b.caps = {"ev": 6.0}
    assert b.step({"L1": 45.0, "L2": 20.0, "L3": 20.0}, WANT, T) is None    # above the release
    assert b.step({"L1": 43.0, "L2": 20.0, "L3": 20.0}, WANT, T + SETTLE) is not None


def test_a_circuit_over_its_limit_moves_only_the_devices_on_it():
    garage = [{"name": "garage", "parent": None, "phases": ["L1", "L2", "L3"], "limit_a": 32.0},
              {"name": "ev-circuit", "parent": "garage", "phases": ["L1", "L2", "L3"],
               "limit_a": 20.0}]
    b = Balancer(budget(circuits=garage, ev_circuit="ev-circuit"))
    # The main is fine; the garage board reads 31 A per phase, over 28.8 A (90 % of 32 A): the EV,
    # on a circuit fed from it, slows by 3 x 2.2 A x 0.23 = 1.518 kW. The battery is not on it.
    d = b.step(LOW, WANT, T, circuit_a={"garage": 31.0})
    assert (d.asset, d.cap_kw) == ("ev", pytest.approx(9.482, abs=0.001))
    assert d.text.startswith("garage at 31.0 A, over 28.8 A (90 % of 32 A)")
    b = Balancer(budget(circuits=garage, ev_circuit=None))
    assert b.step(LOW, WANT, T, circuit_a={"garage": 31.0}) is None   # nothing of Ohmie's on it


def test_a_voltage_sag_slows_a_device_down_like_an_overload():
    b = Balancer(budget())
    assert b.step(LOW, WANT, T, voltage={"L1": 236.0}) is None        # the reference: 236 V
    # 210 V is 11 % below 236 V: one step down (10 % of 11 kW) on the first device that slows.
    d = b.step(LOW, WANT, T + S, voltage={"L1": 210.0})
    assert (d.asset, d.cap_kw) == ("ev", pytest.approx(9.9))
    assert d.text == "L1 at 210 V, 11 % below 236 V: ev slowed from 11.0 to 9.9 kW"
    # Below the absolute floor (230 V - 6 %) counts too, whatever the reference.
    assert Balancer(budget()).step(LOW, WANT, T, voltage={"L1": 215.0}).asset == "ev"
    # A sag never switches a relay off: it is the chargers' and batteries' job.
    b = Balancer(budget())
    b.caps = {"ev": 0.0, "battery": 0.0}
    assert b.step({"L2": 20.0}, WANT, T, voltage={"L2": 214.0}) is None
    # Power comes back only when the voltage is within half the sag of its reference.
    b = Balancer(budget())
    b.step(LOW, WANT, T, voltage={"L1": 236.0, "L2": 236.0, "L3": 236.0})
    b.caps = {"ev": 6.0}
    assert b.step(LOW, WANT, T + SETTLE, voltage={"L1": 220.0, "L2": 236.0, "L3": 236.0}) is None
    assert b.step(LOW, WANT, T + 2 * SETTLE,
                  voltage={"L1": 230.0, "L2": 236.0, "L3": 236.0}).outcome == "restored"


def test_a_cap_the_plan_no_longer_needs_is_dropped_and_no_reading_releases_nothing():
    b = Balancer(budget())
    b.caps = {"battery": 1.0, "ev": 6.0}
    assert b.step({"L1": 20.0}, {"battery": 0.0, "ev": 11.0}, T) is None
    assert b.caps == {"ev": 6.0}                                      # L2, L3 unread: EV holds
