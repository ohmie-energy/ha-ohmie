"""The declarations the wizard writes, checked against the CRD schemas themselves (the gitops repo's
crds/, next to this repo or at GITOPS_CRDS). Skipped when the CRDs are not there."""

from __future__ import annotations

import os
import pathlib

import jsonschema
import pytest
import yaml

from custom_components.ohmie.specs import (
    DAYS,
    STYLES,
    asset_spec,
    binding,
    dns_label,
    preference_spec,
    requirement,
    site_spec,
    supply_name,
    supply_spec,
    targets_spec,
    unique_name,
)

CRDS = pathlib.Path(os.environ.get("GITOPS_CRDS")
                    or pathlib.Path(__file__).parents[2] / "gitops" / "crds")


def check(kind_file: str, spec: dict) -> None:
    path = CRDS / f"{kind_file}.ohmie.energy.yaml"
    if not path.exists():
        pytest.skip(f"no CRDs at {CRDS} (set GITOPS_CRDS)")
    schema = yaml.safe_load(path.read_text())["spec"]["versions"][0]["schema"][
        "openAPIV3Schema"]["properties"]["spec"]
    jsonschema.Draft4Validator(schema).validate(spec)  # OpenAPI 3.0 schemas are draft 4 shaped


SITE = site_spec(timezone="Pacific/Auckland", phases="3P", breaker_a=63, export_kw=5.0,
                 node="MGM0331", island="NI")


def test_a_site_has_its_limits_from_the_breaker():
    check("sites", SITE)
    assert SITE["powerLimits"]["aggregate"] == {"importKw": 43.5, "exportKw": 5.0}
    assert SITE["powerLimits"]["perPhase"] == [  # the CEL rules: one per phase, distinct
        {"phase": p, "importKw": 14.5, "exportKw": 1.67} for p in ("L1", "L2", "L3")]
    assert SITE["memberRef"] == "member" and SITE["islandNode"] == "NI"
    one = site_spec(timezone="Pacific/Auckland", phases="1P", breaker_a=63, export_kw=5.0,
                    node=None, island="SI")
    check("sites", one)
    assert [p["phase"] for p in one["powerLimits"]["perPhase"]] == ["L1"]
    assert "gxpNode" not in one


@pytest.mark.parametrize(("device_type", "answers", "expect"), [
    ("battery", {"storage": {"roundtripEfficiencyPct": 90}, "capacity_kwh": 11.5,
                 "power_kw": 10, "reserve_pct": 25},
     {"scheduler": "storage", "phase": "L1L2L3",
      "storage": {"energyCapacityKwh": 11.5, "powerCapacityKw": 10, "consumptionCapacityKw": 10,
                  "productionCapacityKw": 10, "socMinPct": 25, "socMaxPct": 100,
                  "roundtripEfficiencyPct": 90}}),
    ("ev", {"capacity_kwh": 75, "power_kw": 7.4, "phases": 1},
     {"scheduler": "storage", "phase": "L1",
      "storage": {"energyCapacityKwh": 75, "powerCapacityKw": 7.4, "consumptionCapacityKw": 7.4,
                  "socMinPct": 0, "socMaxPct": 100}}),
    ("hotwater", {"storage": {"energyCapacityKwh": 7.3, "thermal": {"minTempC": 50,
                                                                    "maxTempC": 75}},
                  "power_kw": 3},
     {"scheduler": "storage", "phase": "L1",
      "storage": {"energyCapacityKwh": 7.3, "powerCapacityKw": 3, "consumptionCapacityKw": 3,
                  "socMinPct": 0, "socMaxPct": 100,
                  "thermal": {"minTempC": 50, "maxTempC": 75}}}),
    ("flexload", {"power_kw": 0.8, "run_hours": 4, "every_days": 10},
     {"scheduler": "process", "phase": "L1",
      "flexload": {"powerKw": 0.8, "processType": "shiftable", "durationMinutes": 240,
                   "cycle": {"everyDays": 10}}}),
    ("pv", {"peak_kw": 6.6}, {"scheduler": "inflexible", "phase": "L1L2L3",
                              "pv": {"peakPowerKw": 6.6}}),
    ("meter", {}, {"scheduler": "inflexible", "phase": "L1L2L3"}),
])
def test_asset_specs(device_type, answers, expect):
    spec = asset_spec(device_type, "home", "3P", answers)
    check("assets", spec)
    assert spec == {"siteRef": "home", "deviceType": device_type, **expect}


def test_a_one_phase_site_puts_everything_on_l1():
    assert asset_spec("battery", "home", "1P", {"capacity_kwh": 5, "power_kw": 3})["phase"] == "L1"


def test_targets_merge_into_the_sites_policy_and_keep_the_rest():
    ev = requirement("ev", "ev", {"target_pct": 80, "ready_by": "07:30"})
    hw = requirement("hotwater", "hotwater", {"ready_by": "16:00"})
    assert requirement("pump", "flexload", {}) is None
    existing = {"targetRef": {"kind": "Site", "name": "home"},
                "sitePower": {"perPhase": [{"phase": "L1", "importLimitKw": 5,
                                            "exportLimitKw": 3.5}]},
                "requirements": [{"name": "old-ev", "assetRef": "ev", "kind": "socMinima",
                                  "valuePct": 40, "atLocalTime": "07:00",
                                  "recurrence": "weekdays"}]}
    spec = targets_spec("home", [ev, hw], existing)
    check("constraintpolicies", spec)
    assert spec["sitePower"] == existing["sitePower"]
    assert [(r["assetRef"], r["valuePct"], r["atLocalTime"]) for r in spec["requirements"]] == [
        ("ev", 80, "07:30"), ("hotwater", 100, "16:00")]
    assert targets_spec("home", [ev], None)["targetRef"] == {"kind": "Site", "name": "home"}


@pytest.mark.parametrize("style", list(STYLES))
def test_each_style_is_a_preset(style):
    check("preferencemodes", preference_spec("home", style))


def test_bindings_carry_the_entities_ohmie_reads_and_their_options():
    b = binding("ev", {"soc": "sensor.car_soc"}, {"phases": 3, "power_kw": 11}, {})
    assert b == {"device_type": "ev", "entities": {"soc": "sensor.car_soc"},
                 "options": {"phases": 3, "max_kw": 11}}
    pump = binding("flexload", {"switch": "switch.p"}, {"normal_state": "off", "power_kw": 0.8},
                   {"power": -1})
    assert pump["options"] == {"normal_state": "off", "max_kw": 0.8, "signs": {"power": -1}}


def test_names_are_dns_labels_and_unique():
    assert dns_label("Bach  Hot-Water!") == "bach-hot-water"
    assert dns_label("") == "device"
    assert unique_name("battery", {"battery", "battery-2"}) == "battery-3"


def test_a_tou_plan_from_the_bill():
    spec = supply_spec("home", "import", "2026-10-09", {
        "kind": "schedule", "retailer": "A retailer", "daily_charge": 2.30, "other_c": 34.5,
        "windows": [{"name": "night", "rate_c": 17.25, "from": "21:00", "to": "07:00",
                     "days": "all"},
                    {"name": "weekend", "rate_c": 25, "from": "07:00", "to": "21:00",
                     "days": "weekends"}]})
    check("supplycontracts", spec)
    assert spec["pricing"]["otherRatePerKwh"] == 0.345 and spec["gst"] == "included"
    assert spec["pricing"]["windows"][0] == {"name": "night", "ratePerKwh": 0.1725,
                                             "days": list(DAYS), "from": "21:00", "to": "07:00"}
    assert spec["pricing"]["windows"][1]["days"] == ["sat", "sun"]
    assert supply_name("home", "import", "2026-10-09") == "home-import-20261009"


def test_a_spot_plan_and_a_buyback():
    spot = supply_spec("home", "import", "2026-10-09", {
        "kind": "spot", "retailer": "PowerHub", "margin_c": 2.3, "daily_charge": 1.5})
    check("supplycontracts", spot)
    assert spot["pricing"] == {"kind": "spot"} and spot["fees"] == [
        {"name": "margin", "perKwh": 0.023}]
    buyback = supply_spec("home", "export", "2026-10-09", {
        "kind": "flat", "retailer": "PowerHub", "rate_c": 12})
    check("supplycontracts", buyback)
    assert buyback["pricing"] == {"kind": "flat", "ratePerKwh": 0.12}
    assert "fees" not in buyback and "dailyCharge" not in buyback


def test_ten_windows_a_controlled_register_and_a_percentage_margin():
    windows = [{"name": f"w{i}", "rate_c": 10 + i, "from": f"{i:02d}:00", "to": f"{i + 1:02d}:00",
                "days": "all"} for i in range(10)]
    tou = supply_spec("home", "import", "2026-10-09", {
        "kind": "schedule", "other_c": 30, "windows": windows})
    check("supplycontracts", tou)
    assert len(tou["pricing"]["windows"]) == 10
    ctrl = supply_spec("home", "controlled", "2026-10-09", {
        "kind": "flat", "rate_c": 19, "assets": ["hotwater"], "guaranteed_hours": 17,
        "daily_charge": 2.0})
    check("supplycontracts", ctrl)
    assert (ctrl["flow"], ctrl["register"], ctrl["assetRefs"], ctrl["guaranteedHoursPerDay"]) == (
        "import", "controlled", ["hotwater"], 17)
    assert "dailyCharge" not in ctrl                     # the daily charge is the main plan's
    assert supply_name("home", "controlled", "2026-10-09") == "home-controlled-20261009"
    spot = supply_spec("home", "export", "2026-10-09", {
        "kind": "spot", "margin_c": 1, "margin_pct": 5})
    check("supplycontracts", spot)
    assert spot["fees"] == [{"name": "margin", "perKwh": 0.01},
                            {"name": "margin-pct", "pctOfEnergy": 5}]


def test_changing_a_device_keeps_every_field_it_does_not_ask_for():
    from custom_components.ohmie.specs import change_fields, changed_spec

    battery = {"siteRef": "home", "deviceType": "battery", "phase": "L1", "scheduler": "storage",
               "storage": {"energyCapacityKwh": 10, "powerCapacityKw": 3, "socMinPct": 20,
                           "socMaxPct": 100, "consumptionCapacityKw": 3,
                           "productionCapacityKw": 3, "roundTripEfficiency": 0.9}}
    assert change_fields(battery) == {"capacity_kwh": 10, "power_kw": 3, "reserve_pct": 20}
    out = changed_spec(battery, {"capacity_kwh": 13.8, "power_kw": 5.0, "reserve_pct": 15})
    assert out["storage"] == {"energyCapacityKwh": 13.8, "powerCapacityKw": 5.0,
                              "consumptionCapacityKw": 5.0, "productionCapacityKw": 5.0,
                              "socMinPct": 15, "socMaxPct": 100, "roundTripEfficiency": 0.9}
    assert battery["storage"]["energyCapacityKwh"] == 10           # the input is not changed
    pump = {"siteRef": "home", "deviceType": "flexload", "flexload": {
        "powerKw": 1.0, "processType": "shiftable", "durationMinutes": 120,
        "cycle": {"everyDays": 7, "earliestLocal": "09:00"}}}
    assert change_fields(pump) == {"power_kw": 1.0, "run_hours": 2.0, "every_days": 7}
    assert changed_spec(pump, {"power_kw": 0.8, "run_hours": 4, "every_days": 10})["flexload"] == {
        "powerKw": 0.8, "processType": "shiftable", "durationMinutes": 240,
        "cycle": {"everyDays": 10, "earliestLocal": "09:00"}}
    roof = {"siteRef": "home", "deviceType": "pv", "pv": {"peakPowerKw": 5, "tiltDeg": 20}}
    assert changed_spec(roof, {"peak_kw": 6.6})["pv"] == {"peakPowerKw": 6.6, "tiltDeg": 20}
    assert change_fields({"deviceType": "meter"}) == {}
