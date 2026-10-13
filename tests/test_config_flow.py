"""The setup wizard, end to end against a mocked Ohmie: sign-in (PKCE), a new home, a new site, two
devices found in Home Assistant, the control choice and the terms, and the wait until Ohmie applied
it."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ohmie import config_flow
from custom_components.ohmie.const import API_URL, AUTHORIZE_URL, CLIENT_ID, DOMAIN, TOKEN_URL

from .conftest import fake_jwt

M = f"{API_URL}/v1/members"
CATALOG = {
    "version": 1,
    "profiles": {},
    "integrations": {
        "shelly": {"label": "Shelly", "candidates": [
            {"device_type": "hotwater",
             "entities": {"switch": {"domain": "switch", "pattern": ".*"}}},
            {"device_type": "flexload",
             "entities": {"switch": {"domain": "switch", "pattern": ".*"},
                          "power": {"domain": "sensor", "pattern": "_power$"}}}]},
        "solax_modbus": {"label": "SolaX Inverter Modbus", "candidates": [
            {"device_type": "battery", "entities": {
                "soc": {"domain": "sensor", "pattern": "battery_capacity$"},
                "power": {"domain": "sensor", "pattern": "battery_power_charge$"}}}]},
    },
    "models": [{"id": "tank-pump-800w", "label": "Pump", "device_type": "flexload",
                "spec": {"flexload": {"powerKw": 0.8, "durationMinutes": 240,
                                      "cycle": {"everyDays": 10}}}},
               {"id": "solax-5", "label": "SolaX 5.8", "device_type": "battery",
                "spec": {"storage": {"energyCapacityKwh": 5.8, "powerCapacityKw": 5,
                                     "roundtripEfficiencyPct": 90}}}],
}


def _registry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    devs, ents = dr.async_get(hass), er.async_get(hass)

    def device(ident: str, name: str, platform: str, ids: list[tuple[str, str]]) -> None:
        d = devs.async_get_or_create(config_entry_id=entry.entry_id,
                                     identifiers={(platform, ident)}, name=name)
        for domain, object_id in ids:
            ents.async_get_or_create(domain, platform, f"{ident}-{object_id}",
                                     suggested_object_id=object_id, device_id=d.id)

    device("pump", "Pump relay", "shelly", [("switch", "pump_relay"),
                                            ("sensor", "pump_relay_power")])
    device("inv", "Inverter", "solax_modbus", [
        ("sensor", "solax_battery_capacity"), ("sensor", "solax_battery_power_charge")])


async def _signed_in(hass, hass_client_no_auth, aioclient_mock) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    query = parse_qs(urlparse(result["url"]).query)
    assert result["url"].startswith(AUTHORIZE_URL) and query["client_id"] == [CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"] and "offline_access" in query["scope"][0]
    state = config_entry_oauth2_flow._encode_jwt(hass, {
        "flow_id": result["flow_id"], "redirect_uri": query["redirect_uri"][0]})
    client = await hass_client_no_auth()
    assert (await client.get(f"/auth/external/callback?code=abcd&state={state}")).status == 200
    aioclient_mock.post(TOKEN_URL, json={"access_token": fake_jwt(), "refresh_token": "r",
                                         "token_type": "Bearer", "expires_in": 1800})
    return result


async def _through_progress(hass, flow):
    """Submit the summary; follow the apply progress step if Home Assistant shows it."""
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    return result


@pytest.fixture(autouse=True)
def fast_apply():
    with patch.object(config_flow, "APPLY_POLL", 0):
        yield


async def test_a_new_member_sets_up_a_site_with_two_devices(
        hass, hass_client_no_auth, aioclient_mock, current_request_with_host):
    hass.config.latitude, hass.config.longitude = -40.7, 175.6
    _registry(hass)
    aioclient_mock.get(M, json={"members": []})
    aioclient_mock.get(f"{API_URL}/v1/catalog", json=CATALOG)
    aioclient_mock.post(M, status=201, json={"member": "b2c4d6", "namespace": "member-b2c4d6",
                                             "role": "owner", "state": "active"})
    aioclient_mock.post(f"{API_URL}/v1/locate", json={
        "node": "MGM0331", "island": "NI", "distance_km": 4.6,
        "alternatives": [{"node": "HAY2201", "island": "NI", "distance_km": 80.0}]})
    today = dt_util.now().date().isoformat().replace("-", "")
    for seg, name in (("sites", "home"), ("assets", "pump"), ("assets", "battery"),
                      ("preferences", "home-mode"), ("supply", f"home-import-{today}"),
                      ("supply", f"home-export-{today}"), ("supply", f"home-controlled-{today}")):
        aioclient_mock.put(f"{M}/b2c4d6/{seg}/{name}", status=202,
                           json={"kind": seg, "name": name, "version": 1, "status": "pending"})
    aioclient_mock.get(f"{API_URL}/v1/consent/current",
                       json={"policy_version": "consent-v0", "text": "Placeholder terms."})
    aioclient_mock.post(f"{M}/b2c4d6/control", json={"member": "b2c4d6", "kill_switch": False,
                                                     "consent": {}})
    aioclient_mock.get(f"{M}/b2c4d6/status", json={"member": "b2c4d6", "objects": [
        {"kind": k, "name": n, "version": 1, "status": "applied", "message": None,
         "at": "2026-10-08T00:00:00Z"}
        for k, n in (("Site", "home"), ("Asset", "pump"), ("Asset", "battery"),
                     ("PreferenceMode", "home-mode"), ("SupplyContract", f"home-import-{today}"),
                     ("SupplyContract", f"home-export-{today}"),
                     ("SupplyContract", f"home-controlled-{today}"))]})

    flow = await _signed_in(hass, hass_client_no_auth, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["step_id"] == "new_home"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert result["step_id"] == "site"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "name": "Home", "node": "MGM0331", "icp": "0001234567 pc0f1", "phases": "1P",
        "breaker_a": 63, "export_kw": 5})
    assert result["step_id"] == "devices"
    ids = [o["value"] for o in result["data_schema"].schema["add"].config["options"]]
    assert len(ids) == 2
    result = await hass.config_entries.flow.async_configure(flow["flow_id"],
                                                            {"add": ids, "manual": False})
    # the Shelly relay can be hot water or a pump: say which
    jobs = []
    while result["type"] is FlowResultType.FORM and result["step_id"].startswith("device"):
        step = result["step_id"]
        jobs.append(step)
        if step == "device_type":
            answer = {"device_type": "flexload"}
        elif step == "device_model":
            answer = {"model": result["data_schema"].schema["model"].config["options"][0][
                "value"]}
        else:
            keys = {str(k) for k in result["data_schema"].schema}
            answer = {"name": "pump", "switch": "switch.pump_relay",
                      "power": "sensor.pump_relay_power", "power_kw": 0.8, "run_hours": 4,
                      "every_days": 10, "normal_state": "off"} if "switch" in keys else {
                "name": "battery", "soc": "sensor.solax_battery_capacity",
                "power": "sensor.solax_battery_power_charge", "capacity_kwh": 5.8,
                "power_kw": 5, "reserve_pct": 30}
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], answer)
    assert jobs.count("device") == 2 and "device_type" in jobs
    assert result["step_id"] == "style"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"style": "save"})
    assert result["step_id"] == "plan"
    assert "controlled" in {str(k) for k in result["data_schema"].schema}  # the pump can be
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "kind": "schedule", "retailer": "A retailer", "daily_charge": 2.3,
        "export_kind": "flat", "since": dt_util.now().date().isoformat(), "controlled": True})
    assert result["step_id"] == "plan_schedule"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"other_c": 34.5})
    assert result["step_id"] == "plan_window"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "name": "night", "rate_c": 17.25, "from": "21:00:00", "to": "21:00:00", "days": "all",
        "add_another": True})
    assert result["errors"] == {"base": "empty_window"}
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "name": "night", "rate_c": 17.25, "from": "21:00:00", "to": "07:00:00", "days": "all",
        "add_another": True})
    assert result["step_id"] == "plan_window"                    # window 2
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "name": "shoulder", "rate_c": 25, "from": "07:00:00", "to": "09:00:00",
        "days": "weekdays", "add_another": False})
    assert result["step_id"] == "plan_flat"                      # the buyback
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"rate_c": 12})
    assert result["step_id"] == "plan_controlled"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "assets": ["pump"], "controlled_kind": "flat", "guaranteed_hours": 17})
    assert result["step_id"] == "plan_flat"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"rate_c": 19})
    assert result["step_id"] == "control"
    driveable = [o["value"] for o in result["data_schema"].schema["drive"].config["options"]]
    assert sorted(driveable) == ["battery", "pump"]  # commands too: not only what Ohmie switches
    result = await hass.config_entries.flow.async_configure(flow["flow_id"],
                                                            {"drive": ["pump", "battery"]})
    assert result["step_id"] == "consent"
    assert "Placeholder terms." in result["description_placeholders"]["terms"]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"accept": False})
    assert result["errors"] == {"accept": "must_accept"}
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"accept": True})
    assert result["step_id"] == "summary"
    with patch("custom_components.ohmie.async_setup_entry", return_value=True), \
            patch("custom_components.ohmie.async_unload_entry", return_value=True):
        result = await _through_progress(hass, flow)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert (data["member"], data["site"], data["node"], data["accepted_terms"]) == (
        "b2c4d6", "home", "MGM0331", "consent-v0")
    assert data["bindings"]["pump"] == {
        "device_type": "flexload",
        "entities": {"switch": "switch.pump_relay", "power": "sensor.pump_relay_power"},
        "options": {"normal_state": "off", "max_kw": 0.8}}
    assert data["bindings"]["battery"]["entities"]["soc"] == "sensor.solax_battery_capacity"

    sent = {str(url): (body, headers) for method, url, body, headers in aioclient_mock.mock_calls
            if method in ("PUT", "POST")}
    site, _ = sent[f"{M}/b2c4d6/sites/home"]
    assert site["icp"] == "0001234567PC0F1" and site["spec"]["gxpNode"] == "MGM0331"
    pump, _ = sent[f"{M}/b2c4d6/assets/pump"]
    assert pump["spec"]["flexload"]["durationMinutes"] == 240
    battery, _ = sent[f"{M}/b2c4d6/assets/battery"]
    assert battery["spec"]["storage"]["socMinPct"] == 30
    assert sent[f"{M}/b2c4d6/preferences/home-mode"][0]["spec"]["preset"] == "maximise-savings"
    plan = sent[f"{M}/b2c4d6/supply/home-import-{today}"][0]["spec"]
    assert plan["pricing"]["kind"] == "schedule" and plan["dailyCharge"] == 2.3
    assert [w["name"] for w in plan["pricing"]["windows"]] == ["night", "shoulder"]
    assert plan["pricing"]["windows"][1]["days"] == ["mon", "tue", "wed", "thu", "fri"]
    ctrl = sent[f"{M}/b2c4d6/supply/home-controlled-{today}"][0]["spec"]
    assert (ctrl["register"], ctrl["assetRefs"], ctrl["pricing"]["ratePerKwh"]) == (
        "controlled", ["pump"], 0.19)
    assert sent[f"{M}/b2c4d6/supply/home-export-{today}"][0]["spec"]["pricing"] == {
        "kind": "flat", "ratePerKwh": 0.12}
    grants = [b for m, u, b, _ in aioclient_mock.mock_calls
              if m == "POST" and str(u) == f"{M}/b2c4d6/control"]
    assert grants == [{"kind": "consent", "asset": a, "value": True,
                       "policy_version": "consent-v0"} for a in ("battery", "pump")]
    assert f"{API_URL}/v1/locate" in sent and not urlparse(f"{API_URL}/v1/locate").query


async def test_no_invite_aborts_and_raises_a_repair(
        hass, hass_client_no_auth, aioclient_mock, current_request_with_host):
    aioclient_mock.get(M, status=403,
                       json={"detail": "the member routes needs the pro tier; this token has "
                                       "free"})
    flow = await _signed_in(hass, hass_client_no_auth, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "invite_pending"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "invite_pending") is not None


async def test_a_refusal_from_ohmie_aborts_with_its_reason(
        hass, hass_client_no_auth, aioclient_mock, current_request_with_host):
    hass.config.latitude, hass.config.longitude = -40.7, 175.6
    aioclient_mock.get(M, json={"members": [{"member": "b2c4d6", "namespace": "member-b2c4d6",
                                             "role": "owner", "state": "active"}]})
    aioclient_mock.get(f"{API_URL}/v1/catalog", json=CATALOG)
    aioclient_mock.get(f"{M}/b2c4d6/declarations", json={"member": "b2c4d6",
                                                         "declarations": []})
    aioclient_mock.post(f"{API_URL}/v1/locate", status=422, json={"detail": "outside NZ"})
    aioclient_mock.put(f"{M}/b2c4d6/sites/bach", status=202,
                       json={"kind": "Site", "name": "bach", "version": 1})
    aioclient_mock.put(f"{M}/b2c4d6/preferences/bach-mode", status=202,
                       json={"kind": "PreferenceMode", "name": "bach-mode", "version": 1})
    aioclient_mock.get(f"{M}/b2c4d6/status", json={"member": "b2c4d6", "objects": [
        {"kind": "Site", "name": "bach", "version": 1, "status": "rejected",
         "message": "islandNode SI does not match the node", "at": "2026-10-08T00:00:00Z"}]})
    flow = await _signed_in(hass, hass_client_no_auth, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["step_id"] == "site"  # an owner's single home is picked; no site yet
    assert "island" in {str(k) for k in result["data_schema"].schema}  # locate failed
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "name": "bach", "island": "SI", "icp": "nope", "phases": "1P", "breaker_a": 63,
        "export_kw": 5})
    assert result["errors"] == {"icp": "invalid_icp"}
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {
        "name": "bach", "island": "SI", "phases": "1P", "breaker_a": 63, "export_kw": 5})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"manual": False})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"],
                                                            {"style": "balanced"})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"kind": "later"})
    assert result["step_id"] == "summary"  # nothing to drive: no control step
    result = await _through_progress(hass, flow)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "rejected"
    assert "does not match" in result["description_placeholders"]["problems"]
