"""Finding candidate devices: entity registry rows matched against the catalogue."""

from __future__ import annotations

from custom_components.ohmie.scan import Row, match

CATALOG = {
    "profiles": {},
    "integrations": {
        "solax_modbus": {"label": "SolaX", "candidates": [
            {"device_type": "battery", "entities": {
                "soc": {"domain": "sensor", "pattern": "battery_capacity$"}}},
            {"device_type": "meter", "entities": {
                "power": {"domain": "sensor", "pattern": "measured_power$", "sign": -1}}}]},
        "shelly": {"label": "Shelly", "candidates": [
            {"device_type": "hotwater",
             "entities": {"switch": {"domain": "switch", "pattern": ".*"},
                          "power": {"domain": "sensor", "pattern": "_power$"}}},
            {"device_type": "flexload",
             "entities": {"switch": {"domain": "switch", "pattern": ".*"}}}]},
    },
}

ROWS = [
    Row("sensor.solax_battery_capacity", "solax_modbus", "inv", "Inverter"),
    Row("select.solax_remotecontrol_power_control", "solax_modbus", "inv", "Inverter"),
    Row("button.solax_remotecontrol_trigger", "solax_modbus", "inv", "Inverter"),
    Row("sensor.solax_measured_power", "solax_modbus", "inv", "Inverter"),
    Row("switch.pump_relay", "shelly", "s1", "Pump relay"),
    Row("sensor.pump_relay_power", "shelly", "s1", "Pump relay"),
    Row("switch.lonely", "tuya", "t1", "Plug"),                  # not in this catalogue
    Row("select.other_remotecontrol_power_control", "solax_modbus", "inv2", "Half inverter"),
]


def test_each_device_type_of_an_inverter_is_a_candidate():
    found = {c.id: c for c in match(ROWS, CATALOG)}
    bat = found["solax_modbus:inv:battery"]
    assert bat.device_types == ("battery",) and bat.label == "Inverter (SolaX)"
    assert bat.entities == {"soc": "sensor.solax_battery_capacity"}
    meter = found["solax_modbus:inv:meter"]
    assert meter.device_types == ("meter",) and meter.signs == {"power": -1}
    assert not [c for c in found.values() if c.device_id in ("inv2", "t1")]  # nothing it reads


def test_one_switch_device_is_one_candidate_that_can_be_several_device_types():
    relay = next(c for c in match(ROWS, CATALOG) if c.device_id == "s1")
    assert relay.device_types == ("hotwater", "flexload")
    assert relay.entities == {"switch": "switch.pump_relay", "power": "sensor.pump_relay_power"}


def test_the_energy_dashboard_gives_solar_and_grid_with_its_export_counter():
    from custom_components.ohmie.scan import from_energy_prefs

    prefs = {"energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.pv_total"},
        {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid_in"}],
         "flow_to": [{"stat_energy_to": "sensor.grid_out"}]}]}
    got = {c.id: (c.device_types, dict(c.entities)) for c in from_energy_prefs(prefs)}
    assert got == {"energy:solar": (("pv",), {"energy": "sensor.pv_total"}),
                   "energy:grid": (("meter",), {"energy": "sensor.grid_in",
                                                "energy_out": "sensor.grid_out"})}
