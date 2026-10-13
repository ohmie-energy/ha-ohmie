"""The declarations the wizard writes (CR specs, exactly as the CRDs define them) and the bindings
it keeps in the config entry. No Home Assistant imports."""

from __future__ import annotations

import copy
import re
from typing import Any

VOLTS = 230
# The three styles, in plain words, and the PreferenceMode preset each one is.
STYLES = {
    "save": "maximise-savings",
    "balanced": "balanced",
    "backup": "backup-rural",
}
SCHEDULER = {"battery": "storage", "ev": "storage", "hotwater": "storage", "flexload": "process",
             "pv": "inflexible", "meter": "inflexible"}
WHOLE_SITE = {"battery", "pv", "meter"}  # on every phase of a 3-phase Site


def dns_label(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:63].strip("-")
    return s or "device"


def unique_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def site_spec(*, timezone: str, phases: str, breaker_a: float, export_kw: float,
              node: str | None, island: str) -> dict[str, Any]:
    """Import limits from the main breaker on each phase; the export limit shared between them.
    The CRD's CEL rules: one per-phase limit per phase, L1 first."""
    n = 3 if phases == "3P" else 1
    per_phase_kw = round(breaker_a * VOLTS / 1000, 1)
    spec: dict[str, Any] = {
        "memberRef": "member", "timezone": timezone, "phaseTopology": phases,
        "powerLimits": {
            "aggregate": {"importKw": round(per_phase_kw * n, 1), "exportKw": export_kw},
            "perPhase": [{"phase": f"L{i + 1}", "importKw": per_phase_kw,
                          "exportKw": round(export_kw / n, 2)} for i in range(n)]},
        "mainBreakerA": breaker_a, "islandNode": island}
    if node:
        spec["gxpNode"] = node
    return spec


def _phase(device_type: str, site_phases: str, answers: dict[str, Any]) -> str:
    if site_phases != "3P":
        return "L1"
    if device_type in WHOLE_SITE or int(answers.get("phases") or 1) == 3:
        return "L1L2L3"
    return "L1"


def asset_spec(device_type: str, site: str, site_phases: str,
               answers: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {"siteRef": site, "deviceType": device_type,
                            "phase": _phase(device_type, site_phases, answers),
                            "scheduler": SCHEDULER[device_type]}
    kw = answers.get("power_kw")
    if device_type in ("battery", "ev", "hotwater"):
        storage = {**(answers.get("storage") or {})}
        if answers.get("capacity_kwh") is not None:
            storage["energyCapacityKwh"] = answers["capacity_kwh"]
        if kw is not None:
            storage.update(powerCapacityKw=kw, consumptionCapacityKw=kw)
            if device_type == "battery":
                storage["productionCapacityKw"] = kw
        if device_type == "battery":
            storage["socMinPct"] = answers.get("reserve_pct", 0)
        storage.setdefault("socMinPct", 0)
        storage.setdefault("socMaxPct", 100)
        spec["storage"] = storage
    elif device_type == "flexload":
        spec["flexload"] = {"powerKw": kw, "processType": "shiftable",
                            "durationMinutes": round(float(answers["run_hours"]) * 60),
                            "cycle": {"everyDays": int(answers["every_days"])}}
        for key, field in (("earliest", "earliestLocal"), ("latest", "latestLocal")):
            if answers.get(key):
                spec["flexload"]["cycle"][field] = answers[key]
    elif device_type == "pv":
        spec["pv"] = {"peakPowerKw": answers["peak_kw"]}
    return spec


def change_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """The specs a member can correct on an existing device, with their current values."""
    dt = spec.get("deviceType")
    st, fl = spec.get("storage") or {}, spec.get("flexload") or {}
    if dt in ("battery", "ev", "hotwater"):
        out = {"capacity_kwh": st.get("energyCapacityKwh"), "power_kw": st.get("powerCapacityKw")}
        if dt == "battery":
            out["reserve_pct"] = st.get("socMinPct", 0)
        return out
    if dt == "flexload":
        return {"power_kw": fl.get("powerKw"),
                "run_hours": (fl.get("durationMinutes") or 0) / 60,
                "every_days": (fl.get("cycle") or {}).get("everyDays")}
    if dt == "pv":
        return {"peak_kw": (spec.get("pv") or {}).get("peakPowerKw")}
    return {}


def changed_spec(spec: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """The device's spec with the corrected values; every other field kept as declared."""
    out = copy.deepcopy(spec)
    dt = out.get("deviceType")
    if dt in ("battery", "ev", "hotwater"):
        st = out.setdefault("storage", {})
        st["energyCapacityKwh"] = answers["capacity_kwh"]
        kw = answers["power_kw"]
        st.update(powerCapacityKw=kw, consumptionCapacityKw=kw)
        if dt == "battery":
            st["productionCapacityKw"] = kw
            st["socMinPct"] = answers["reserve_pct"]
    elif dt == "flexload":
        fl = out.setdefault("flexload", {})
        fl["powerKw"] = answers["power_kw"]
        fl["durationMinutes"] = round(float(answers["run_hours"]) * 60)
        fl.setdefault("cycle", {})["everyDays"] = int(answers["every_days"])
    elif dt == "pv":
        out.setdefault("pv", {})["peakPowerKw"] = answers["peak_kw"]
    return out


def requirement(asset: str, device_type: str, answers: dict[str, Any]) -> dict[str, Any] | None:
    """A ready-by target: the EV at least at its target %, the hot water full."""
    if device_type == "ev":
        return {"name": f"{asset}-ready", "assetRef": asset, "kind": "socMinima",
                "valuePct": answers.get("target_pct", 80),
                "atLocalTime": answers.get("ready_by", "07:00"), "recurrence": "daily"}
    if device_type == "hotwater":
        return {"name": f"{asset}-ready", "assetRef": asset, "kind": "socTarget",
                "valuePct": 100, "atLocalTime": answers.get("ready_by", "16:00"),
                "recurrence": "daily"}
    return None


def targets_spec(site: str, reqs: list[dict[str, Any]],
                 existing: dict[str, Any] | None) -> dict[str, Any]:
    """The Site's ConstraintPolicy with these targets; anything else in it is kept."""
    spec = dict(existing or {"targetRef": {"kind": "Site", "name": site}})
    assets = {r["assetRef"] for r in reqs}
    kept = [r for r in spec.get("requirements") or [] if r.get("assetRef") not in assets]
    spec["requirements"] = kept + reqs
    return spec


def preference_spec(site: str, style: str) -> dict[str, Any]:
    return {"targetRef": {"kind": "Site", "name": site}, "preset": STYLES[style]}


def binding(device_type: str, entities: dict[str, str], answers: dict[str, Any],
            signs: dict[str, int]) -> dict[str, Any]:
    """The entities Ohmie reads for a device (never drives), and its options."""
    options: dict[str, Any] = {}
    for key in ("normal_state", "phases"):
        if answers.get(key) not in (None, ""):
            options[key] = answers[key]
    if answers.get("power_kw") is not None:
        options["max_kw"] = answers["power_kw"]
    if signs:
        options["signs"] = signs
    return {"device_type": device_type, "entities": dict(entities), "options": options}


# --- the member's power plan (SupplyContract, ADR D10) ---
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SETS = {"all": list(DAYS), "weekdays": list(DAYS[:5]), "weekends": list(DAYS[5:])}


def _nzd(cents: float) -> float:
    return round(float(cents) / 100, 6)


def supply_name(site: str, key: str, valid_from: str) -> str:
    return f"{site}-{key}-{valid_from.replace('-', '')}"


def supply_spec(site: str, key: str, valid_from: str,
                answers: dict[str, Any]) -> dict[str, Any]:
    """A SupplyContract from what the member reads off the bill: c/kWh and $/day, GST included
    (as bills show them). key: import, export, or controlled (a ripple-controlled register beside
    the main import plan, naming its devices). kind: spot (plus a margin per kWh and/or a
    percentage of the energy price), schedule (windows + the other hours), flat."""
    kind = answers["kind"]
    spec: dict[str, Any] = {"siteRef": site, "flow": "export" if key == "export" else "import",
                            "validFrom": valid_from,
                            "party": {"name": answers.get("retailer") or "my retailer"},
                            "gst": "included" if answers.get("gst_included", True) else "excluded",
                            "pricing": {"kind": kind}}
    if key == "controlled":
        spec["register"] = "controlled"
        spec["assetRefs"] = list(answers["assets"])
        if answers.get("guaranteed_hours"):
            spec["guaranteedHoursPerDay"] = float(answers["guaranteed_hours"])
    if key == "import" and answers.get("daily_charge"):
        spec["dailyCharge"] = float(answers["daily_charge"])
    fees = []
    if kind == "spot" and answers.get("margin_c"):
        fees.append({"name": "margin", "perKwh": _nzd(answers["margin_c"])})
    if kind == "spot" and answers.get("margin_pct"):
        fees.append({"name": "margin-pct", "pctOfEnergy": float(answers["margin_pct"])})
    if fees:
        spec["fees"] = fees
    if kind == "flat":
        spec["pricing"]["ratePerKwh"] = _nzd(answers["rate_c"])
    if kind == "schedule":
        spec["pricing"]["otherRatePerKwh"] = _nzd(answers["other_c"])
        spec["pricing"]["windows"] = [
            {"name": w.get("name") or f"window-{i + 1}", "ratePerKwh": _nzd(w["rate_c"]),
             "days": DAY_SETS[w.get("days") or "all"], "from": w["from"], "to": w["to"]}
            for i, w in enumerate(answers.get("windows") or [])]
    return spec
