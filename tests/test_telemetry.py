"""Telemetry: readings from entity states, batches, and the queue of unsent batches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ohmie.model import Binding, EntityState
from custom_components.ohmie.telemetry import MAX_PENDING, Batch, Batcher, Queue, reading

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
BAT = Binding("battery", "battery",
              {"soc": "sensor.soc", "power": "sensor.bat_power", "other": "number.x"})
METER = Binding("meter", "meter", {"power": "sensor.grid", "energy": "sensor.grid_in"},
                {"signs": {"power": -1}})


def s(state, unit=None):
    return EntityState(state, {"unit_of_measurement": unit} if unit else {})


def test_a_reading_takes_the_telemetry_roles_in_ohmies_units():
    r = reading(BAT, {"sensor.soc": s("63.5", "%"), "sensor.bat_power": s("-1200", "W"),
                      "number.x": s("100")}, T0)
    assert r == {"time": "2026-10-08T09:00:00+00:00", "soc_pct": 63.5, "power_kw": -1.2}


@pytest.mark.parametrize(("state", "unit", "field", "value"), [
    ("2.5", "kW", "power_kw", -2.5),                     # SolaX meter: + is export, Ohmie: + import
    ("1500", "W", "power_kw", -1.5),
    ("10231.4", "kWh", "energy_kwh", 10231.4),
    ("10231400", "Wh", "energy_kwh", 10231.4),
])
def test_units_and_signs(state, unit, field, value):
    ent = "sensor.grid" if field == "power_kw" else "sensor.grid_in"
    r = reading(METER, {ent: s(state, unit)}, T0)
    assert r[field] == pytest.approx(value)


def test_fahrenheit_becomes_celsius():
    b = Binding("hotwater", "hotwater", {"temp": "sensor.t", "switch": "switch.x"})
    assert reading(b, {"sensor.t": s("140", "°F")}, T0)["temp_c"] == pytest.approx(60.0)


def test_no_usable_value_is_no_reading():
    assert reading(BAT, {"sensor.soc": s("unavailable", "%"), "sensor.bat_power": s("x", "W")},
                   T0) is None
    assert reading(BAT, {}, T0) is None
    assert reading(BAT, {"sensor.soc": s("150", "%")}, T0) is None  # out of range: dropped


def test_batches_split_at_1000_readings_and_carry_a_content_hash_id():
    b = Batcher()
    for i in range(1500):
        b.add("battery", {"time": (T0 + timedelta(minutes=i)).isoformat(), "soc_pct": 50.0})
    b.add("meter", {"time": T0.isoformat(), "power_kw": 1.0})
    out = b.flush(T0)
    assert [(x.asset, len(x.payload["readings"])) for x in out] == [
        ("battery", 1000), ("battery", 500), ("meter", 1)]
    assert out[0].payload["v"] == 1 and out[0].payload["type"] == "telemetry"
    same = Batcher()
    same.add("meter", {"time": T0.isoformat(), "power_kw": 1.0})
    assert same.flush(T0 + timedelta(hours=1))[0].msg_id == out[2].msg_id  # a replay dedupes
    assert b.flush(T0) == []


def test_the_queue_keeps_order_drops_the_oldest_when_full_and_round_trips():
    q = Queue()
    for i in range(MAX_PENDING + 2):
        q.add(Batch("battery", f"id{i}", {"readings": [{"time": str(i)}]}))
    assert len(q) == MAX_PENDING and q.peek().msg_id == "id2"
    q.done("id2")
    assert q.peek().msg_id == "id3"
    again = Queue.from_list(q.to_list())
    assert again.peek() == q.peek() and len(again) == len(q)


def test_a_relay_with_no_power_sensor_reports_its_rated_power_while_on():
    b = Binding("pump", "flexload", {"switch": "switch.relay"}, {"max_kw": 0.8})
    assert reading(b, {"switch.relay": s("on")}, T0)["power_kw"] == 0.8
    assert reading(b, {"switch.relay": s("off")}, T0)["power_kw"] == 0.0
    metered = Binding("pump", "flexload", {"switch": "switch.relay", "power": "sensor.p"},
                      {"max_kw": 0.8})
    assert reading(metered, {"switch.relay": s("on"), "sensor.p": s("750", "W")},
                   T0)["power_kw"] == 0.75              # a real reading wins


def test_pv_readings_are_always_production_whatever_the_sensor_sign():
    # Ohmie's convention: production is negative. No PV sensor counts it so; some catalogue ones
    # (SolaX) carry sign -1 already. Either way the edge sends a negative power and counter.
    roof = Binding("roof", "pv", {"power": "sensor.pv", "energy": "sensor.pv_total"})
    r = reading(roof, {"sensor.pv": s("3000", "W"), "sensor.pv_total": s("1234.5", "kWh")}, T0)
    assert (r["power_kw"], r["energy_kwh"]) == (-3.0, -1234.5)
    signed = Binding("roof", "pv", {"power": "sensor.pv"}, {"signs": {"power": -1}})
    assert reading(signed, {"sensor.pv": s("3000", "W")}, T0)["power_kw"] == -3.0


def test_a_meters_counter_is_import_less_export():
    m = Binding("meter", "meter", {"energy": "sensor.grid_in", "energy_out": "sensor.grid_out"})
    r = reading(m, {"sensor.grid_in": s("100", "kWh"), "sensor.grid_out": s("40000", "Wh")}, T0)
    assert r == {"time": T0.isoformat(), "energy_kwh": 60.0}
    # Half of the pair is no net counter: the export counter alone is not sent.
    assert reading(m, {"sensor.grid_out": s("40", "kWh")}, T0) is None


def test_a_circuits_voltage_is_sent_in_volts():
    hw = Binding("hotwater", "hotwater", {"voltage": "sensor.hw_v", "temp": "sensor.t"})
    r = reading(hw, {"sensor.hw_v": s("0.236", "kV"), "sensor.t": s("55", "°C")}, T0)
    assert r["voltage_v"] == pytest.approx(236.0) and r["temp_c"] == 55.0
    assert reading(hw, {"sensor.hw_v": s("3", "V")}, T0)["voltage_v"] == 3.0
