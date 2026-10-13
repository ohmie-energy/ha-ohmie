"""The blueprints, run as real automations: Ohmie's entities change, the member's devices get the
calls. Brand-neutral by design: the device entities here are plain selects, numbers and times."""

from __future__ import annotations

import pathlib
import shutil

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

DIR = pathlib.Path(__file__).parents[1] / "blueprints" / "automation" / "ohmie"
SHIPPED = {"mirror_switch.yaml", "notify_actions.yaml", "mode_and_power.yaml",
           "charge_window.yaml", "ev_charge_current.yaml", "weekly_report.yaml"}


def test_every_blueprint_is_tested_here():
    assert {p.name for p in DIR.glob("*.yaml")} == SHIPPED


async def _automations(hass: HomeAssistant, *uses: tuple[str, dict]) -> None:
    dest = pathlib.Path(hass.config.path("blueprints", "automation", "ohmie"))
    dest.mkdir(parents=True, exist_ok=True)
    for name, _ in uses:
        shutil.copy(DIR / name, dest / name)
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": name, "use_blueprint": {"path": f"ohmie/{name}", "input": inputs}}
        for name, inputs in uses]})
    await hass.async_block_till_done()
    for name, _ in uses:  # each one loaded: a broken template leaves it unavailable
        assert hass.states.get(f"automation.{name.removesuffix('.yaml')}") is not None or \
            any(s.attributes.get("id") == name for s in hass.states.async_all("automation"))


def calls_for(hass, *services):
    return {s: async_mock_service(hass, *s.split(".")) for s in services}


async def _command(hass, state, watts=0, entity="sensor.ohmie_battery_command"):
    hass.states.async_set(entity, state, {"power_kw": watts / 1000, "power_w": watts,
                                          "power_magnitude_w": abs(watts)})
    await hass.async_block_till_done()


@pytest.fixture
def battery_inputs():
    return {"command": "sensor.ohmie_battery_command", "mode": "select.inv_mode",
            "charge_option": "Force charge", "discharge_option": "Force discharge",
            "normal_option": "Self use", "power": "number.inv_power", "power_unit": "kW"}


async def test_mode_and_power_follows_the_command(hass, battery_inputs):
    c = calls_for(hass, "select.select_option", "number.set_value")
    await _automations(hass, ("mode_and_power.yaml", battery_inputs))
    await _command(hass, "charge", 2500)
    assert c["select.select_option"][-1].data["option"] == "Force charge"
    assert c["number.set_value"][-1].data == {"entity_id": ["number.inv_power"], "value": 2.5}
    await _command(hass, "discharge", -4000)
    assert c["select.select_option"][-1].data["option"] == "Force discharge"
    assert c["number.set_value"][-1].data["value"] == 4.0
    await _command(hass, "hold")
    assert c["select.select_option"][-1].data["option"] == "Self use"  # no hold option: normal
    await _command(hass, "normal")
    assert c["select.select_option"][-1].data["option"] == "Self use"
    assert len(c["number.set_value"]) == 2


async def test_turning_the_members_own_condition_off_hands_the_battery_back(hass,
                                                                             battery_inputs):
    c = calls_for(hass, "select.select_option", "number.set_value")
    hass.states.async_set("input_boolean.ohmie_enabled", "on")
    await _automations(hass, ("mode_and_power.yaml",
                              {**battery_inputs, "enabled": "input_boolean.ohmie_enabled"}))
    await _command(hass, "charge", 2500)
    assert c["select.select_option"][-1].data["option"] == "Force charge"
    hass.states.async_set("input_boolean.ohmie_enabled", "off")
    await hass.async_block_till_done()
    assert c["select.select_option"][-1].data["option"] == "Self use"   # at once
    await _command(hass, "discharge", -3000)                             # Ohmie keeps planning
    assert c["select.select_option"][-1].data["option"] == "Self use"   # but nothing passes
    hass.states.async_set("input_boolean.ohmie_enabled", "on")
    await hass.async_block_till_done()
    assert c["select.select_option"][-1].data["option"] == "Force discharge"


async def test_charge_window_copies_the_window_or_turns_it_off(hass):
    await hass.config.async_set_time_zone("Pacific/Auckland")
    c = calls_for(hass, "time.set_value", "number.set_value", "switch.turn_on",
                  "switch.turn_off")
    await _automations(hass, ("charge_window.yaml", {
        "window": "sensor.ohmie_battery_next_charge", "start": "time.slot_start",
        "end": "time.slot_end", "target": "number.slot_soc", "enable": "switch.slot"}))
    hass.states.async_set("sensor.ohmie_battery_next_charge", "2026-10-08T12:30:00+00:00", {
        "end": "2026-10-08T14:00:00+00:00", "target_soc_pct": 81.4, "commanded": True})
    await hass.async_block_till_done()
    assert [x.data["time"] for x in c["time.set_value"]] == ["01:30:00", "03:00:00"]  # NZDT
    assert c["number.set_value"][-1].data["value"] == 81
    assert len(c["switch.turn_on"]) == 1
    hass.states.async_set("sensor.ohmie_battery_next_charge", "2026-10-08T12:30:00+00:00", {
        "end": "2026-10-08T14:00:00+00:00", "target_soc_pct": 81.4, "commanded": False})
    await hass.async_block_till_done()
    assert [x.data["time"] for x in c["time.set_value"][-2:]] == ["00:00:00", "00:00:00"]
    assert len(c["switch.turn_off"]) == 1 and len(c["number.set_value"]) == 1


async def test_ev_charge_current_sends_only_changes(hass):
    c = calls_for(hass, "number.set_value", "switch.turn_on", "switch.turn_off")
    hass.states.async_set("switch.car_charge", "off")
    hass.states.async_set("number.car_current", "32")
    await _automations(hass, ("ev_charge_current.yaml", {
        "command": "sensor.ohmie_ev_command", "charge": "switch.car_charge",
        "current": "number.car_current"}))
    await _command(hass, "charge", 3680, "sensor.ohmie_ev_command")       # 16 A at 230 V
    assert c["number.set_value"][-1].data["value"] == 16 and len(c["switch.turn_on"]) == 1
    hass.states.async_set("switch.car_charge", "on")
    hass.states.async_set("number.car_current", "16")
    await _command(hass, "charge", 3700, "sensor.ohmie_ev_command")       # still 16 A
    assert len(c["number.set_value"]) == 1 and len(c["switch.turn_on"]) == 1
    await _command(hass, "hold", 0, "sensor.ohmie_ev_command")
    assert len(c["switch.turn_off"]) == 1
    hass.states.async_set("switch.car_charge", "off")
    await _command(hass, "normal", 0, "sensor.ohmie_ev_command")          # the car's own way
    assert c["number.set_value"][-1].data["value"] == 32 and len(c["switch.turn_on"]) == 2


async def test_mirror_switch_and_notify(hass):
    c = calls_for(hass, "switch.turn_on", "switch.turn_off", "notify.mobile_app_phone")
    hass.states.async_set("sensor.ohmie_pump_reason", "Cheapest 4 h before Sunday")
    hass.states.async_set("input_boolean.ohmie_enabled", "on")
    await _automations(
        hass,
        ("mirror_switch.yaml", {"source": "switch.ohmie_pump", "target": "switch.pump",
                                "enabled": "input_boolean.ohmie_enabled"}),
        ("notify_actions.yaml", {"action_sensor": "sensor.ohmie_pump_action",
                                 "reason_sensor": "sensor.ohmie_pump_reason",
                                 "notify_action": "notify.mobile_app_phone"}))
    hass.states.async_set("switch.ohmie_pump", "on")
    hass.states.async_set("sensor.ohmie_pump_action", "run")
    await hass.async_block_till_done()
    assert c["switch.turn_on"][-1].data["entity_id"] == ["switch.pump"]
    assert c["notify.mobile_app_phone"][-1].data["message"] == "Cheapest 4 h before Sunday"
    hass.states.async_set("input_boolean.ohmie_enabled", "off")
    hass.states.async_set("switch.ohmie_pump", "off")
    await hass.async_block_till_done()
    assert c["switch.turn_off"] == []                      # the member's condition holds it
    hass.states.async_set("input_boolean.ohmie_enabled", "on")
    await hass.async_block_till_done()
    assert c["switch.turn_off"][-1].data["entity_id"] == ["switch.pump"]  # catches up


async def test_weekly_report_notifies_once_per_new_week(hass):
    c = calls_for(hass, "notify.mobile_app_phone")
    sensor = "sensor.ohmie_home_left_on_the_table"
    await _automations(hass, ("weekly_report.yaml", {"report_sensor": sensor,
                                                     "notify_action": "notify.mobile_app_phone"}))
    week = {"week_start": "2026-10-12", "paid": 69.0, "daily_charges": 16.1, "spot": 59.8,
            "ohmie": 54.63, "unit_of_measurement": "NZD"}
    hass.states.async_set(sensor, "14.37", week)
    await hass.async_block_till_done()
    msg = c["notify.mobile_app_phone"][-1].data["message"]
    assert "$69.00" in msg and "$59.80" in msg and "$14.37" in msg
    hass.states.async_set(sensor, "14.40", week)            # late prices revise the same week
    await hass.async_block_till_done()
    assert len(c["notify.mobile_app_phone"]) == 1
    hass.states.async_set(sensor, "3.10", {**week, "week_start": "2026-10-19", "spot": None})
    await hass.async_block_till_done()
    assert len(c["notify.mobile_app_phone"]) == 2
    assert "spot" not in c["notify.mobile_app_phone"][-1].data["message"].lower()

