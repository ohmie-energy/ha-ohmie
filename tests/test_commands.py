"""The command a member's automation follows, and the window-and-target-SoC shape."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.ohmie.commands import Command, command, next_window, output_on
from custom_components.ohmie.edge import Consent, Control, Period, Plan

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
GRANT = Control(consent={"battery": Consent(True, "consent-v0", T0)})


def at(m):
    return T0 + timedelta(minutes=m)


def plan(kws, delivery="hands-off"):
    def action(kw):
        return "charge" if kw > 0.05 else "discharge" if kw < -0.05 else "hold"

    return Plan("battery", "s1", T0, delivery, "Consented",
                tuple(Period(at(30 * i), kw, action(kw)) for i, kw in enumerate(kws)))


def test_hands_off_commands_the_current_period():
    c = command(plan([3.0, -2.0]), GRANT, "battery", False, None, at(10))
    assert (c.state, c.power_kw, c.until) == ("charge", 3.0, at(30))
    assert command(plan([3.0, -2.0]), GRANT, "battery", False, None, at(40)).state == "discharge"


def test_anything_short_of_hands_off_is_normal():
    p = plan([3.0])
    assert command(plan([3.0], "advisory"), GRANT, "battery", False, None, at(5)).state == "normal"
    assert command(p, Control(kill_switch=True, consent=GRANT.consent), "battery", False, None,
                   at(5)).state == "normal"
    assert command(p, Control(), "battery", False, None, at(5)).state == "normal"
    assert command(p, GRANT, "battery", True, None, at(5)).state == "normal"      # paused here
    assert command(p, GRANT, "battery", False, None, at(45)).state == "normal"    # plan ran out
    assert command(None, GRANT, "battery", False, None, at(5)).state == "normal"
    c = command(p, GRANT, "battery", False, at(30), at(5))                        # overridden
    assert c.state == "normal" and c.reason == "overridden"


def test_the_next_charge_window_carries_its_target_soc():
    p = plan([0.0, 2.5, 2.5, 0.0, -5.0, -5.0])
    w = next_window(p, at(5), {"charge", "run"}, soc_now=40.0, capacity_kwh=10.0)
    assert (w.start, w.end, w.power_kw) == (at(30), at(90), 2.5)
    assert w.energy_kwh == 2.5 and w.target_soc_pct == 65.0       # 40 % + 2.5 kWh of 10 kWh
    e = next_window(p, at(5), {"discharge"}, soc_now=40.0, capacity_kwh=10.0)
    assert (e.start, e.end, e.power_kw, e.target_soc_pct) == (at(120), at(180), -5.0, 15.0)
    inside = next_window(p, at(45), {"charge", "run"}, soc_now=50.0, capacity_kwh=10.0)
    assert inside.start == at(30) and inside.target_soc_pct == 68.8  # half this period + the next


def test_no_soc_or_capacity_means_no_target():
    p = plan([2.0])
    assert next_window(p, at(5), {"charge"}, soc_now=None, capacity_kwh=10).target_soc_pct is None
    assert next_window(p, at(40), {"charge"}, soc_now=50, capacity_kwh=10) is None


def test_the_ohmie_switch_is_on_to_run_or_charge_and_shows_normal_otherwise():
    assert output_on(Command("run", 0.8), "off") is True
    assert output_on(Command("charge", 3.0), "off") is True
    assert output_on(Command("idle"), "on") is False
    assert output_on(Command("normal"), "on") is True      # the member's normal state
    assert output_on(Command("normal"), "off") is False


def test_a_load_balancing_cap_lowers_only_a_consuming_command():
    from datetime import UTC, datetime

    from custom_components.ohmie.commands import Command, capped

    until = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)
    ev = Command("charge", 11.0, until)
    assert capped(ev, None) is ev
    assert capped(ev, 6.5) == Command("charge", 6.5, until, "load balancing")
    assert capped(ev, 0.0) == Command("hold", 0.0, until, "load balancing")
    assert capped(Command("run", 0.8, until), 0.0) == Command("idle", 0.0, until, "load balancing")
    out = Command("discharge", -5.0, until)
    assert capped(out, 0.0) is out                 # export is not limited here
    assert capped(Command("normal"), 0.0) == Command("normal")   # Ohmie is not in control
