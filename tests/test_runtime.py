"""A set-up entry with a fake NATS link: the entities, Ohmie driving a pump relay, a manual change,
the pause, telemetry and acks out, and diagnostics."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ohmie.const import API_URL, DOMAIN
from custom_components.ohmie.diagnostics import async_get_config_entry_diagnostics
from custom_components.ohmie.model import period_floor

from .conftest import fake_jwt

MID = "b2c4d6"
M = f"{API_URL}/v1/members/{MID}"


class FakeLink:
    instances: list[FakeLink] = []

    def __init__(self, url, subjects, token, on_message, on_connect, on_state):
        self.on_message, self.on_connect = on_message, on_connect
        self.connected, self.down_since = False, time.monotonic()
        self.state: dict[str, dict] = {}
        self.published: list[tuple[str, dict, str]] = []
        FakeLink.instances.append(self)

    async def start(self):
        self.connected, self.down_since = True, None
        await self.on_connect()

    async def stop(self):
        self.connected = False

    async def last(self, subject):
        return self.state.get(subject)

    async def publish(self, subject, payload, msg_id):
        self.published.append((subject, payload, msg_id))
        return True


def plan(actions: list[str], delivery: str = "hands-off", asset: str = "pump",
         kw: float = 0.8) -> dict[str, Any]:
    start = period_floor(dt_util.utcnow())
    return {"v": 1, "type": "plan", "ts": start.isoformat(), "asset": asset,
            "schedule_uuid": "s1", "computed_at": start.isoformat(),
            "effective_delivery": delivery, "control_reason": "Consented",
            "periods": [{"start": (start + timedelta(minutes=30 * i)).isoformat(),
                         "setpoint_kw": kw if a in ("run", "charge") else 0.0, "action": a,
                         "reason": "the cheapest 4 h before the tank runs low"}
                        for i, a in enumerate(actions)]}


def REPORT_WEEK(start, left):  # noqa: N802 - a fixture builder, like CONTROL
    def money(v):
        return {"ex_gst": v, "incl_gst": round(v * 1.15, 4)}
    return {"week_start": start.isoformat(), "periods": 336, "covered": 330, "import_kwh": 150.0,
            "export_kwh": 40.0, "paid": money(60.0), "daily": money(14.0), "spot": money(52.0),
            "ohmie": money(60.0 - left), "left_on_table": money(left)}


BUDGET = {"v": 1, "type": "budget", "site": "home", "volts": 230.0, "limit_pct": 90,
          "watch_pct": 60, "phases": {"L1": {"import_a": 63.0, "export_a": 21.7}},
          "devices": [{"asset": "battery", "phases": ["L1"], "max_kw": 5.0, "min_kw": 0.0,
                       "modulates": True, "priority": 2},
                      {"asset": "pump", "phases": ["L1"], "max_kw": 0.8, "min_kw": 0.8,
                       "modulates": False, "priority": 3}]}


CONTROL = {"v": 1, "type": "control", "ts": "2026-10-08T00:00:00Z", "kill_switch": False,
           "consent": {a: {"granted": True, "policy_version": "consent-v0",
                           "at": "2026-10-08T00:00:00Z"} for a in ("pump", "battery")}}


@pytest.fixture
async def setup(hass: HomeAssistant, aioclient_mock):
    FakeLink.instances.clear()
    aioclient_mock.get(f"{API_URL}/v1/members", json={"members": [
        {"member": MID, "namespace": f"member-{MID}", "role": "owner", "state": "active"}]})
    aioclient_mock.get(f"{M}/declarations", json={"member": MID, "declarations": [
        {"kind": "Site", "name": "home", "version": 1, "spec": {
            "phaseTopology": "1P", "circuits": [{"name": "garage", "limitA": 32, "phase": "L1"}]}},
        {"kind": "Asset", "name": "pump", "version": 2, "spec": {
            "siteRef": "home", "deviceType": "flexload",
            "flexload": {"powerKw": 0.8, "durationMinutes": 240, "cycle": {"everyDays": 10}}}},
        {"kind": "Asset", "name": "battery", "version": 1, "spec": {
            "siteRef": "home", "deviceType": "battery",
            "storage": {"energyCapacityKwh": 10, "socMinPct": 20}}}]})
    aioclient_mock.get(f"{API_URL}/v1/consent/current",
                       json={"policy_version": "consent-v0", "text": "t"})
    aioclient_mock.get(f"{API_URL}/v1/forecast/MGM0331?horizon=24h", json={"periods": [
        {"start": period_floor(dt_util.utcnow()).isoformat(), "price_p10": 50.0,
         "price_p50": 80.0, "price_p90": 120.0, "label": "cheap", "source": "wits"}]})
    # The member is on a ToU plan: this period is its peak (dear), whatever the market says.
    aioclient_mock.get(f"{M}/sites/home/price?hours=24", json={
        "member": MID, "site": "home", "unit": "NZD/MWh, ex GST", "periods": [
            {"start": period_floor(dt_util.utcnow()).isoformat(), "import_price": 300.0,
             "export_price": 104.35, "import_basis": "schedule", "export_basis": "flat",
             "import_contract": "home-import-20261009", "export_contract": "buyback",
             "import_window": "peak", "label": "dear", "lines_known": None,
             "controlled_price": 170.0, "controlled_contract": "home-controlled-20261009",
             "nodal_price": None, "loss_factor": None, "import_lines": None,
             "export_lines": None}]})
    today = dt_util.now().date()
    monday = today - timedelta(days=today.weekday())
    aioclient_mock.get(f"{M}/sites/home/report?weeks=2", json={
        "member": MID, "site": "home", "currency": "NZD", "gst_rate": 0.15, "notes": [],
        "weeks": [REPORT_WEEK(monday - timedelta(days=7), 12.5), REPORT_WEEK(monday, 1.0)]})
    aioclient_mock.post(f"{M}/control", json={"member": MID, "kill_switch": True,
                                              "consent": CONTROL["consent"]})
    hass.states.async_set("switch.pump_relay", "off")
    hass.states.async_set("sensor.pump_relay_power", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.bat_soc", "40", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.grid_l1", "20", {"unit_of_measurement": "A"})
    aioclient_mock.get(f"{M}/sites/home/budget", json=BUDGET)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"{MID}-home", data={
        "auth_implementation": DOMAIN,
        "token": {"access_token": fake_jwt(), "refresh_token": "r", "token_type": "Bearer",
                  "expires_at": time.time() + 1800},
        "member": MID, "site": "home", "role": "owner", "node": "MGM0331",
        "balance": {"L1": "sensor.grid_l1"},
        "accepted_terms": "consent-v0",
        "bindings": {"pump": {"device_type": "flexload",
                              "entities": {"switch": "switch.pump_relay",
                                           "power": "sensor.pump_relay_power"},
                              "options": {"normal_state": "off", "max_kw": 0.8}},
                     "battery": {"device_type": "battery",
                                 "entities": {"soc": "sensor.bat_soc"}, "options": {}}}})
    entry.add_to_hass(hass)
    # The member's real devices: no service call may ever target them (ADR D9).
    real: list[Any] = []

    def watch(event):
        ids = event.data.get("service_data", {}).get("entity_id") or []
        ids = [ids] if isinstance(ids, str) else ids
        real.extend(i for i in ids if not i.split(".", 1)[1].startswith("ohmie_"))

    unwatch = hass.bus.async_listen("call_service", watch)
    with patch("custom_components.ohmie.runtime.NatsLink", FakeLink):
        # Ohmie's last plan and control state are waiting in EDGE_STATE when the link connects.
        original = FakeLink.__init__

        def seeded(self, *a, **kw):
            original(self, *a, **kw)
            self.state = {f"ohmie.m.{MID}.sig.pump": plan(["run", "run", "idle"]),
                          f"ohmie.m.{MID}.sig.battery": plan(["hold", "charge", "charge"],
                                                             asset="battery", kw=2.5),
                          f"ohmie.m.{MID}.ctl.member": CONTROL}

        with patch.object(FakeLink, "__init__", seeded):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    yield entry, FakeLink.instances[0]
    await hass.config_entries.async_unload(entry.entry_id)
    unwatch()
    assert real == []  # Ohmie never touched a real device


async def test_entities_show_the_plan_mode_and_prices(hass, setup):
    st = hass.states.get
    assert st("sensor.ohmie_pump_action").state == "run"
    assert st("sensor.ohmie_pump_setpoint").state == "0.8"
    assert st("sensor.ohmie_pump_mode").state == "hands-off"
    assert st("binary_sensor.ohmie_pump_run_now").state == "on"
    assert st("switch.ohmie_pump_let_ohmie_drive").state == "on"
    assert st("number.ohmie_pump_run_hours").state == "4.0"
    price = st("sensor.ohmie_home_price_now")
    assert price.state == "0.3" and price.attributes["basis"].startswith("your plan")
    assert (price.attributes["plan"], price.attributes["window"]) == ("schedule", "peak")
    assert price.attributes["export"] == 0.10435 and price.attributes["controlled"] == 0.17
    assert st("binary_sensor.ohmie_home_cheap_now").state == "off"  # the plan's label wins
    assert st("binary_sensor.ohmie_home_connected").state == "on"
    assert st("calendar.ohmie_home_plan").attributes["message"] == "pump: run"
    left = st("sensor.ohmie_home_left_on_the_table")      # last complete week, incl GST
    assert left.state == "14.375" and left.attributes["unit_of_measurement"] == "NZD"
    assert (left.attributes["paid"], left.attributes["spot"]) == (69.0, 59.8)
    assert left.attributes["this_week_so_far"] == 1.15


async def test_the_ohmie_switch_follows_the_plan_and_the_real_relay_is_left_alone(hass, setup):
    entry, _ = setup
    pump = hass.states.get("switch.ohmie_pump")
    assert pump.state == "on" and pump.attributes["command"] == "run"
    assert hass.states.get("switch.pump_relay").state == "off"   # untouched: the member wires it


async def test_switching_the_ohmie_switch_by_hand_is_an_override(hass, setup):
    entry, link = setup
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ohmie_pump"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("switch.ohmie_pump").state == "off"  # the member wins this period
    await entry.runtime_data._send(dt_util.utcnow())
    acks = [p for s, p, _ in link.published if s == f"ohmie.m.{MID}.ack.pump"]
    assert [a["outcome"] for a in acks] == ["overridden"]


async def test_pause_hands_back_at_once_and_records_the_kill_switch(hass, setup, aioclient_mock):
    entry, _ = setup
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": "switch.ohmie_home_pause_ohmie"}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("switch.ohmie_home_pause_ohmie").state == "on"
    assert hass.states.get("switch.ohmie_pump").state == "off"  # the pump's normal state
    assert hass.states.get("sensor.ohmie_pump_mode").state == "advisory"
    posted = [b for m, u, b, _ in aioclient_mock.mock_calls if m == "POST"]
    assert {"kind": "kill_switch", "asset": None, "value": True,
            "policy_version": None} in posted


async def test_telemetry_goes_out_in_ohmies_units(hass, setup):
    entry, link = setup
    hass.states.async_set("sensor.pump_relay_power", "800", {"unit_of_measurement": "W"})
    rt = entry.runtime_data
    await rt._tick(dt_util.utcnow())
    await rt._send(dt_util.utcnow())
    tel = [p for s, p, _ in link.published if s == f"ohmie.m.{MID}.tel.pump"]
    assert tel and tel[0]["readings"][0]["power_kw"] == 0.8


async def test_diagnostics_leave_out_the_token(hass, setup):
    entry, _ = setup
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["token"] == "**REDACTED**"
    assert diag["assets"]["pump"]["mode"] == "hands-off"


async def test_options_revoke_control_without_new_terms(hass, setup, aioclient_mock):
    entry, _ = setup
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    aioclient_mock.get(f"{M}/status", json={"member": MID, "objects": []})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert set(result["menu_options"]) == {"devices", "change", "entities", "power_plan",
                                           "remove", "balancing", "control"}
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "control"})
    assert result["step_id"] == "control"
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                                   {"drive": []})
    assert result["type"] == "create_entry"
    posted = [b for m, u, b, _ in aioclient_mock.mock_calls if m == "POST"]
    assert {"kind": "consent", "asset": "pump", "value": False, "policy_version": None} in posted


async def test_options_remove_a_device(hass, setup, aioclient_mock):
    entry, _ = setup
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    aioclient_mock.delete(f"{M}/assets/pump", status=202,
                          json={"kind": "Asset", "name": "pump", "version": 3})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "remove"})
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                                   {"assets": ["pump"]})
    assert result["type"] == "create_entry"
    assert "pump" not in entry.data["bindings"] and "battery" in entry.data["bindings"]
    assert any(m == "DELETE" for m, *_ in aioclient_mock.mock_calls)


async def test_commands_for_a_device_the_member_wires(hass, setup):
    entry, _ = setup
    await entry.runtime_data._tick(dt_util.utcnow())        # reads the SoC
    await hass.async_block_till_done()
    st = hass.states.get
    assert st("sensor.ohmie_battery_command").state == "hold"
    assert st("sensor.ohmie_pump_command").state == "run"
    nxt = st("sensor.ohmie_battery_next_charge")
    start = period_floor(dt_util.utcnow()) + timedelta(minutes=30)
    assert dt_util.parse_datetime(nxt.state) == start
    assert nxt.attributes["power_kw"] == 2.5 and nxt.attributes["energy_kwh"] == 2.5
    assert nxt.attributes["target_soc_pct"] == 65.0         # 40 % + 2.5 kWh of 10 kWh
    assert st("sensor.ohmie_battery_reserve").state == "20"
    assert st("switch.ohmie_battery_let_ohmie_drive").state == "on"
    await entry.runtime_data.set_pause(True)
    await hass.async_block_till_done()
    assert st("sensor.ohmie_battery_command").state == "normal"


async def test_a_reported_override_hands_the_device_back(hass, setup):
    entry, link = setup
    await hass.services.async_call(DOMAIN, "report", {"asset": "battery",
                                                      "outcome": "overridden",
                                                      "detail": "changed at the inverter"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ohmie_battery_command").state == "normal"
    assert hass.states.get("sensor.ohmie_battery_command").attributes["reason"] == "overridden"
    await entry.runtime_data._send(dt_util.utcnow())
    acks = [p for s, p, _ in link.published if s == f"ohmie.m.{MID}.ack.battery"]
    assert acks[-1]["outcome"] == "overridden" and acks[-1]["detail"] == "changed at the inverter"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "report", {"asset": "nope", "outcome": "failed"},
                                       blocking=True)


async def test_options_change_entities_stays_in_home_assistant(hass, setup, aioclient_mock):
    entry, _ = setup
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    hass.states.async_set("switch.new_relay", "off")
    hass.states.async_set("sensor.new_power", "0")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "entities"})
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"asset": "pump"})
    assert result["step_id"] == "device"
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {
            "switch": "switch.new_relay", "power": "sensor.new_power", "normal_state": "on"})
    assert result["type"] == "create_entry"
    pump = entry.data["bindings"]["pump"]
    assert pump["entities"]["switch"] == "switch.new_relay"
    assert pump["options"] == {"normal_state": "on", "max_kw": 0.8}  # max_kw kept from before
    assert not [m for m, *_ in aioclient_mock.mock_calls if m in ("PUT", "DELETE", "POST")]


async def test_without_a_total_price_the_sensor_shows_the_nodal_price(hass, setup, aioclient_mock):
    entry, _ = setup
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_URL}/v1/forecast/MGM0331?horizon=24h", json={"periods": [
        {"start": period_floor(dt_util.utcnow()).isoformat(), "price_p10": 50.0,
         "price_p50": 80.0, "price_p90": 120.0, "label": "cheap", "source": "wits"}]})
    aioclient_mock.get(f"{M}/sites/home/price?hours=24", status=404,
                       json={"detail": "no total price for this site: it needs an ICP"})
    await entry.runtime_data._forecast_tick(dt_util.utcnow())
    await hass.async_block_till_done()
    price = hass.states.get("sensor.ohmie_home_price_now")
    assert price.state == "0.08" and price.attributes["basis"].startswith("nodal")
    assert hass.states.get("binary_sensor.ohmie_home_cheap_now").state == "on"  # the market's


async def test_options_change_the_power_plan_ends_the_old_one_today(hass, setup, aioclient_mock):
    entry, _ = setup
    rt = entry.runtime_data
    rt.decls[("SupplyContract", "home-import-20260101")] = {
        "kind": "SupplyContract", "name": "home-import-20260101", "version": 2,
        "spec": {"siteRef": "home", "flow": "import", "validFrom": "2026-01-01",
                 "party": {"name": "Old"}, "pricing": {"kind": "flat", "ratePerKwh": 0.3}}}
    today = dt_util.now().date().isoformat()
    new = f"home-import-{today.replace('-', '')}"
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    aioclient_mock.put(f"{M}/supply/home-import-20260101", status=202,
                       json={"kind": "SupplyContract", "name": "old", "version": 3})
    aioclient_mock.put(f"{M}/supply/{new}", status=202,
                       json={"kind": "SupplyContract", "name": new, "version": 1})
    aioclient_mock.get(f"{M}/status", json={"member": MID, "objects": [
        {"kind": "SupplyContract", "name": n, "version": v, "status": "applied",
         "message": None, "at": "2026-10-08T00:00:00Z"}
        for n, v in (("home-import-20260101", 3), (new, 1))]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "power_plan"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "kind": "spot", "retailer": "PowerHub"})
    assert result["step_id"] == "plan_spot"
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"margin_c": 2.3})
    assert result["type"] == "create_entry"
    puts = {str(u): b for m, u, b, _ in aioclient_mock.mock_calls if m == "PUT"}
    assert puts[f"{M}/supply/home-import-20260101"]["spec"]["validTo"] == today
    assert puts[f"{M}/supply/{new}"]["spec"]["fees"] == [{"name": "margin", "perKwh": 0.023}]


async def test_a_plan_corrected_in_the_past_cuts_and_replaces_the_contracts_after_it(
        hass, setup, aioclient_mock):
    entry, _ = setup
    rt = entry.runtime_data
    for name, frm, to, version in (("home-import-20260101", "2026-01-01", None, 2),
                                   ("home-import-20260501", "2026-05-01", None, 1)):
        rt.decls[("SupplyContract", name)] = {
            "kind": "SupplyContract", "name": name, "version": version,
            "spec": {"siteRef": "home", "flow": "import", "validFrom": frm,
                     **({"validTo": to} if to else {}), "party": {"name": "Old"},
                     "pricing": {"kind": "flat", "ratePerKwh": 0.3}}}
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    aioclient_mock.put(f"{M}/supply/home-import-20260101", status=202,
                       json={"kind": "SupplyContract", "name": "a", "version": 3})
    aioclient_mock.delete(f"{M}/supply/home-import-20260501", status=202,
                          json={"kind": "SupplyContract", "name": "b", "version": 2})
    aioclient_mock.put(f"{M}/supply/home-import-20260315", status=202,
                       json={"kind": "SupplyContract", "name": "c", "version": 1})
    aioclient_mock.get(f"{M}/status", json={"member": MID, "objects": [
        {"kind": "SupplyContract", "name": n, "version": v, "status": "applied",
         "message": None, "at": "2026-10-08T00:00:00Z"}
        for n, v in (("home-import-20260101", 3), ("home-import-20260315", 1))]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "power_plan"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "kind": "flat", "retailer": "New", "export_kind": "none", "since": "2099-01-01"})
    assert result["errors"] == {"since": "since_future"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "kind": "flat", "retailer": "New", "export_kind": "none", "since": "2026-03-15"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"rate_c": 28})
    assert result["type"] == "create_entry"
    calls = [(m, str(u)) for m, u, *_ in aioclient_mock.mock_calls if m in ("PUT", "DELETE")]
    assert calls == [("PUT", f"{M}/supply/home-import-20260101"),       # cut on 15 March
                     ("DELETE", f"{M}/supply/home-import-20260501"),    # replaced: it was later
                     ("PUT", f"{M}/supply/home-import-20260315")]
    cut = next(b for m, u, b, _ in aioclient_mock.mock_calls
               if m == "PUT" and str(u).endswith("20260101"))
    assert cut["spec"]["validTo"] == "2026-03-15"


async def test_the_solar_forecast_goes_to_ohmie_for_the_sites_pv(hass, setup, aioclient_mock):
    entry, _ = setup
    rt = entry.runtime_data
    rt.assets["roof"] = "pv"
    aioclient_mock.put(f"{M}/assets/roof/forecast", json={"stored": 2})
    now = dt_util.utcnow()
    hour = now.replace(minute=0, second=0, microsecond=0)
    fc = [{hour.isoformat(): 2000}]
    with patch("custom_components.ohmie.runtime.energy_dashboard_forecasts",
               return_value=(fc, "ha-energy:forecast_solar")):
        await rt._solar_tick(now)
    (body,) = [b for m, u, b, _ in aioclient_mock.mock_calls if m == "PUT"]
    assert body["source"] == "ha-energy:forecast_solar"
    assert [p["power_kw"] for p in body["periods"]] == [-2.0, -2.0]


async def test_a_suspected_cut_asks_the_member_and_the_answer_goes_to_ohmie(
        hass, setup, aioclient_mock):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ohmie.repairs import async_create_fix_flow

    entry, _ = setup
    rt = entry.runtime_data
    start = period_floor(dt_util.utcnow()) - timedelta(hours=2)

    def event(eid, status):
        return {"event_id": eid, "asset": "pump", "started_at": start.isoformat(),
                "ended_at": (start + timedelta(hours=1)).isoformat(), "evidence": "power",
                "notice_id": "CAN-7", "status": status}

    aioclient_mock.get(f"{M}/supply-events", json={"events": [
        event("pump-1", "suspected"), event("pump-2", "measured")]})
    aioclient_mock.post(f"{M}/supply-events/pump-1", json=event("pump-1", "confirmed"))
    await rt._supply_tick(dt_util.utcnow())
    reg = ir.async_get(hass)
    issue_id = f"{entry.entry_id}_supply_pump-1"
    issue = reg.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable and issue.translation_key == "supply_cut"
    assert reg.async_get_issue(DOMAIN, f"{entry.entry_id}_supply_pump-2") is None  # measured

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass, flow.flow_id, flow.handler = hass, "f1", DOMAIN
    assert (await flow.async_step_init())["type"] == "form"
    assert (await flow.async_step_init({"cut": "yes"}))["type"] == "create_entry"
    (body,) = [b for m, u, b, _ in aioclient_mock.mock_calls
               if m == "POST" and str(u).endswith("/supply-events/pump-1")]
    assert body == {"confirmed": True}
    assert reg.async_get_issue(DOMAIN, issue_id) is None


async def test_options_change_a_devices_specs(hass, setup, aioclient_mock):
    entry, _ = setup
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    aioclient_mock.put(f"{M}/assets/battery", status=202,
                       json={"kind": "Asset", "name": "battery", "version": 2})
    aioclient_mock.get(f"{M}/status", json={"member": MID, "objects": [
        {"kind": "Asset", "name": "battery", "version": 2, "status": "applied", "message": ""}]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "change"})
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"asset": "battery"})
    assert result["step_id"] == "change_device"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"capacity_kwh": 13.8, "power_kw": 5.0, "reserve_pct": 15})
    assert result["type"] == "create_entry"
    ((url, body, headers),) = [(str(u), b, h) for m, u, b, h in aioclient_mock.mock_calls
                               if m == "PUT"]
    assert url.endswith("/assets/battery") and headers["If-Match"] == '"1"'
    assert body["spec"]["storage"]["energyCapacityKwh"] == 13.8
    assert body["spec"]["storage"]["socMinPct"] == 15


async def test_load_balancing_stops_the_pump_over_the_ceiling_and_restarts_it(
        hass, setup, freezer, aioclient_mock):
    entry, link = setup
    aioclient_mock.get(f"{M}/supply-events", json={"events": []})  # the clock moves below
    st = hass.states.get
    assert st("sensor.ohmie_home_headroom_l1").state == "43.0"            # 63 A - 20 A
    assert st("binary_sensor.ohmie_home_load_balancing").state == "off"
    assert st("switch.ohmie_pump").state == "on"                          # the plan: run
    decisions = []
    hass.bus.async_listen("ohmie_decision", lambda e: decisions.append(e.data))
    # 60 A on L1, the ceiling is 56.7 A: the battery holds (nothing to give), the pump stops.
    hass.states.async_set("sensor.grid_l1", "60", {"unit_of_measurement": "A"})
    await hass.async_block_till_done()
    assert st("switch.ohmie_pump").state == "off"
    # The member reads why in the Logbook, and Ohmie gets the same reason as an ack.
    assert decisions == [{"asset": "pump", "outcome": "limited", "text":
                          "L1 at 60.0 A, over 56.7 A (90 % of 63 A): pump switched off"}]
    rt = entry.runtime_data
    sent = [p for _, p, _ in link.published] + [i["payload"] for i in rt.ack_queue.to_list()]
    (ack,) = [p for p in sent if p.get("outcome") == "limited"]
    assert ack["detail"].endswith("pump switched off") and ack["setpoint_kw"] == 0.0
    assert st("sensor.ohmie_pump_command").state == "idle"
    assert st("binary_sensor.ohmie_home_load_balancing").state == "on"
    # A power sensor works too (W at 230 V): 4.6 kW = 20 A, under the release line. The engine
    # first waits to see what its last action did (SETTLE), then gives the power back.
    hass.states.async_set("sensor.grid_l1", "4600", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert st("switch.ohmie_pump").state == "off"
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set("sensor.grid_l1", "4610", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert st("switch.ohmie_pump").state == "on"
    assert st("binary_sensor.ohmie_home_load_balancing").state == "off"



async def test_options_set_the_phase_readings_and_the_commit_for_load_balancing(
        hass, setup, aioclient_mock):
    entry, _ = setup
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1, "profiles": {},
                                                     "integrations": {}, "models": []})
    aioclient_mock.put(f"{M}/sites/home", status=202,
                       json={"kind": "Site", "name": "home", "version": 2})
    aioclient_mock.get(f"{M}/status", json={"member": MID, "objects": [
        {"kind": "Site", "name": "home", "version": 2, "status": "applied", "message": ""}]})
    hass.states.async_set("sensor.mains_l1_power", "2300", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "balancing"})
    assert result["step_id"] == "balancing"
    assert set(result["data_schema"].schema) == {
        "L1", "voltage_L1", "circuit_garage", "commit_pct", "release_pct", "sag_pct"}
    answers = {"L1": "sensor.mains_l1_power", "voltage_L1": "sensor.mains_l1_voltage",
               "circuit_garage": "sensor.garage_power", "commit_pct": 80, "sag_pct": 8}
    # The release line at most 15 points below the commit: 60 is refused, 70 is fine.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**answers, "release_pct": 60})
    assert result["errors"] == {"release_pct": "release_gap"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**answers, "release_pct": 70})
    assert result["type"] == "create_entry" and reload.called
    assert entry.data["balance"] == {"L1": "sensor.mains_l1_power"}
    assert entry.data["balance_voltage"] == {"L1": "sensor.mains_l1_voltage"}
    assert entry.data["balance_circuits"] == {"garage": "sensor.garage_power"}
    ((body, headers),) = [(b, h) for m, u, b, h in aioclient_mock.mock_calls
                          if m == "PUT" and str(u).endswith("/sites/home")]
    assert body["spec"]["loadBalancing"] == {"commitPct": 80, "releasePct": 70,
                                             "voltageSagPct": 8}
    assert body["spec"]["phaseTopology"] == "1P" and headers["If-Match"] == '"1"'
