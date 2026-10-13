"""The setup wizard (api-specs/home-assistant-contract.md, "Setup wizard"). One config entry is one
Ohmie Site.

  sign in -> home (create one, or pick a member) -> site (pick one, or add one) -> devices (the
  Site's Ohmie Assets first, then candidates found here, or one by hand) -> per device: type, model,
  details -> style -> control (+ the consent terms) -> summary -> apply (wait until Ohmie applied
  it) -> entry

The options flow reuses the device and control steps."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import oauth
from .api import OhmieApi, OhmieApiError, OhmieNotInvited
from .const import (
    CONF_BALANCE,
    CONF_BALANCE_CIRCUITS,
    CONF_BALANCE_VOLTAGE,
    CONF_BINDINGS,
    CONF_MEMBER,
    CONF_NODE,
    CONF_ROLE,
    CONF_SITE,
    CONF_TERMS,
    DEVICE_TYPES,
    DOMAIN,
    SCOPES,
)
from .runtime import NORMAL_STATE, OUTPUT_TYPES
from .scan import Candidate, energy_candidates, match, registry_rows
from .specs import (
    STYLES,
    asset_spec,
    binding,
    change_fields,
    changed_spec,
    dns_label,
    preference_spec,
    requirement,
    site_spec,
    supply_name,
    supply_spec,
    targets_spec,
    unique_name,
)

_LOG = logging.getLogger(__name__)
APPLY_POLL, APPLY_TRIES = 3.0, 30     # wait up to ~90 s for Ohmie to apply the declarations
NEW = "__new__"
STYLE_WORDS = {
    "save": "Save the most: use cheap power, avoid dear power",
    "balanced": "Balanced: save, and keep some charge in reserve",
    "backup": "Keep a buffer: ready for a power cut",
}
NONE = "none"
ICP = re.compile(r"^\d{10}[A-Z]{2}[0-9A-Z]{3}$")
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ROLE_DOMAIN = {"soc": "sensor", "power": "sensor", "energy": "sensor", "energy_out": "sensor",
               "temp": "sensor", "voltage": "sensor", "switch": "switch"}
TELEMETRY_FOR = {"battery": ("soc", "power"), "ev": ("soc", "power"),
                 "hotwater": ("temp", "power", "voltage"), "flexload": ("power", "voltage"),
                 "pv": ("power", "energy"), "meter": ("power", "energy", "energy_out")}
DRIVEN = {"battery", "ev", "hotwater", "flexload"}


def _select(options: list[tuple[str, str]], multiple: bool = False) -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[selector.SelectOptionDict(value=v, label=lbl) for v, lbl in options],
        multiple=multiple, mode=selector.SelectSelectorMode.LIST))


def _number(lo: float, hi: float, step: float, unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=lo, max=hi, step=step, unit_of_measurement=unit,
        mode=selector.NumberSelectorMode.BOX))


class DeviceSteps:
    """The device, control and consent steps, shared by the wizard and the options flow.
    Needs: self.hass, self._api, self._mid, self._site, self._site_phases, self._decls,
    self._catalog; fills self._assets and self._drive."""

    _api: OhmieApi
    _mid: str
    _site: str
    _site_phases: str
    _role: str
    _decls: list[dict[str, Any]]
    _catalog: dict[str, Any]
    _queue: list[dict[str, Any]]
    _job: dict[str, Any] | None
    _assets: dict[str, dict[str, Any]]
    _drive: set[str]
    _consent: dict[str, Any] | None
    _accepted: str | None
    _candidates: list[Candidate]

    def _init_devices(self) -> None:
        self._queue, self._job, self._assets, self._drive = [], None, {}, set()
        self._consent, self._accepted, self._candidates = None, None, []

    def _declared(self, kind: str) -> dict[str, dict[str, Any]]:
        return {d["name"]: d for d in self._decls if d["kind"] == kind}

    def _site_assets(self) -> dict[str, dict[str, Any]]:
        return {n: d for n, d in self._declared("Asset").items()
                if d["spec"].get("siteRef") == self._site}

    def _taken(self) -> set[str]:
        return set(self._declared("Asset")) | set(self._assets)

    async def _show_devices(self, bound: set[str], step_id: str) -> ConfigFlowResult:
        rows = registry_rows(self.hass)  # type: ignore[attr-defined]
        found = match(rows, self._catalog) + await energy_candidates(self.hass)  # type: ignore
        used = {e for a in self._assets.values() for e in a["binding"]["entities"].values()}
        self._candidates = [c for c in found if not set(c.entities.values()) & used]
        existing = [n for n in self._site_assets() if n not in bound]
        schema: dict[Any, Any] = {}
        if self._candidates:
            schema[vol.Optional("add", default=[c.id for c in self._candidates])] = _select(
                [(c.id, f"{c.label}: {', '.join(c.device_types)}") for c in self._candidates],
                multiple=True)
        schema[vol.Optional("manual", default=not self._candidates and not existing)] = \
            selector.BooleanSelector()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id, data_schema=vol.Schema(schema),
            description_placeholders={"existing": ", ".join(existing) or "none",
                                      "found": str(len(self._candidates))})

    def _queue_devices(self, user_input: dict[str, Any], bound: set[str]) -> None:
        chosen = [c for c in self._candidates if c.id in (user_input.get("add") or [])]
        for name, d in self._site_assets().items():
            if name in bound:
                continue
            dt = d["spec"]["deviceType"]
            cand = next((c for c in chosen if dt in c.device_types), None)
            if cand is not None:
                chosen.remove(cand)
            self._queue.append({"bind": name, "device_type": dt,
                                "entities": dict(cand.entities) if cand else {},
                                "signs": dict(cand.signs) if cand else {},
                                "label": name})
        for c in chosen:
            one = len(c.device_types) == 1
            self._queue.append({"bind": None, "device_type": c.device_types[0] if one else None,
                                "device_types": list(c.device_types),
                                "entities": dict(c.entities), "signs": dict(c.signs),
                                "label": c.label})
        if user_input.get("manual"):
            self._queue.append({"bind": None, "device_type": None,
                                "device_types": list(DEVICE_TYPES),
                                "entities": {}, "signs": {}, "label": "your device"})

    async def _next_device(self) -> ConfigFlowResult:
        if not self._queue:
            return await self._after_devices()
        self._job = self._queue.pop(0)
        if self._job["device_type"] is None:
            return await self.async_step_device_type()
        return await self.async_step_device_model()

    async def _after_devices(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_device_type(self, user_input: dict[str, Any] | None = None
                                     ) -> ConfigFlowResult:
        job = self._job
        assert job is not None
        if user_input is not None:
            job["device_type"] = user_input["device_type"]
            return await self.async_step_device_model()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="device_type", data_schema=vol.Schema({vol.Required("device_type"): _select(
                [(t, t) for t in job["device_types"]])}),
            description_placeholders={"device": job["label"]})

    async def async_step_device_model(self, user_input: dict[str, Any] | None = None
                                      ) -> ConfigFlowResult:
        job = self._job
        assert job is not None
        models = [m for m in self._catalog["models"] if m["device_type"] == job["device_type"]]
        if job["bind"] or not models:
            job["model"] = None
            return await self.async_step_device()
        if user_input is not None:
            job["model"] = next((m for m in models if m["id"] == user_input["model"]), None)
            return await self.async_step_device()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="device_model",
            data_schema=vol.Schema({vol.Required("model", default=models[0]["id"]): _select(
                [(m["id"], m["label"]) for m in models] + [(NONE, "Other: I'll enter it")])}),
            description_placeholders={"device": job["label"], "type": job["device_type"]})

    def _device_schema(self, job: dict[str, Any]) -> dict[Any, Any]:
        dt, model = job["device_type"], job.get("model") or {}
        storage = (model.get("spec") or {}).get("storage") or {}
        flex = (model.get("spec") or {}).get("flexload") or {}
        schema: dict[Any, Any] = {}
        if not job["bind"]:
            schema[vol.Required("name", default=unique_name(dns_label(dt), self._taken()))] = \
                selector.TextSelector()
        roles = list(TELEMETRY_FOR[dt]) + (["switch"] if dt in OUTPUT_TYPES else [])
        for role in roles:  # all read only: Ohmie never drives the member's entities
            hint = {"suggested_value": job["entities"].get(role)}
            schema[vol.Optional(role, description=hint)] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=ROLE_DOMAIN[role]))
        if job["bind"]:
            pass  # the Asset is declared already: only its entities are set here
        elif dt in ("battery", "ev", "hotwater"):
            if dt != "hotwater" or not storage:
                schema[vol.Required("capacity_kwh", default=storage.get("energyCapacityKwh",
                                                                        10))] = \
                    _number(0.5, 500, 0.1, "kWh")
            schema[vol.Required("power_kw", default=storage.get("powerCapacityKw", 3))] = \
                _number(0.1, 100, 0.1, "kW")
        if not job["bind"] and dt == "battery":
            schema[vol.Required("reserve_pct", default=20)] = _number(0, 100, 5, "%")
        if not job["bind"] and dt == "ev":
            schema[vol.Required("target_pct", default=80)] = _number(10, 100, 5, "%")
            schema[vol.Required("ready_by", default="07:00:00")] = selector.TimeSelector()
        if not job["bind"] and dt == "hotwater":
            schema[vol.Required("ready_by", default="16:00:00")] = selector.TimeSelector()
        if not job["bind"] and dt == "flexload":
            schema[vol.Required("power_kw", default=flex.get("powerKw", 1.0))] = \
                _number(0.05, 50, 0.05, "kW")
            schema[vol.Required("run_hours",
                                default=flex.get("durationMinutes", 240) / 60)] = \
                _number(0.5, 24, 0.5, "h")
            schema[vol.Required("every_days",
                                default=(flex.get("cycle") or {}).get("everyDays", 7))] = \
                _number(1, 60, 1, "d")
        if not job["bind"] and dt == "pv":
            schema[vol.Required("peak_kw", default=5.0)] = _number(0.1, 200, 0.1, "kW")
        if dt == "ev" and not job["bind"]:
            schema[vol.Required("phases", default="1")] = _select([("1", "1"), ("3", "3")])
        if dt in OUTPUT_TYPES:
            schema[vol.Required("normal_state", default=(job.get("options") or {}).get(
                "normal_state", NORMAL_STATE[dt]))] = _select([("on", "On"), ("off", "Off")])
        return schema

    async def async_step_device(self, user_input: dict[str, Any] | None = None
                                ) -> ConfigFlowResult:
        job = self._job
        assert job is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            name = job["bind"] or dns_label(user_input.get("name", ""))
            if not job["bind"] and (not LABEL.match(name) or name in self._taken()):
                errors["name"] = "invalid_name"
            else:
                answers = dict(user_input)
                for key in ("ready_by",):
                    if answers.get(key):
                        answers[key] = answers[key][:5]
                if "phases" in answers:
                    answers["phases"] = int(answers["phases"])
                answers["storage"] = ((job.get("model") or {}).get("spec") or {}).get("storage")
                entities = {r: answers[r] for r in ROLE_DOMAIN if answers.get(r)}
                dt = job["device_type"]
                bound = binding(dt, entities, answers, job["signs"])
                bound["options"] = {**job.get("options", {}), **bound["options"]}
                self._assets[name] = {
                    "spec": None if job["bind"] else asset_spec(dt, self._site,
                                                                self._site_phases, answers),
                    "requirement": None if job["bind"] else requirement(name, dt, answers),
                    "binding": bound,
                }
                return await self._next_device()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="device", data_schema=vol.Schema(self._device_schema(job)), errors=errors,
            description_placeholders={"device": job["label"], "type": job["device_type"]})

    # --- control: "Ohmie suggests" (the default) or "Let Ohmie drive" ---

    def _driveable(self) -> list[str]:
        return [n for n, a in self._assets.items() if a["binding"]["device_type"] in DRIVEN]

    async def async_step_control(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        driveable = self._driveable()
        if self._role != "owner" or not driveable:
            return await self._after_control()
        if user_input is not None:
            self._drive = set(user_input.get("drive") or [])
            if self._drive:
                return await self.async_step_consent()
            return await self._after_control()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="control", data_schema=vol.Schema({
                vol.Optional("drive", default=sorted(self._drive)): _select(
                    [(n, n) for n in driveable], multiple=True)}))

    async def async_step_consent(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        if self._consent is None:
            self._consent = await self._api.consent()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("accept"):
                self._accepted = self._consent["policy_version"]
                return await self._after_control()
            errors["accept"] = "must_accept"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="consent", errors=errors,
            data_schema=vol.Schema({vol.Required("accept", default=False):
                                    selector.BooleanSelector()}),
            description_placeholders={"terms": self._consent["text"],
                                      "version": self._consent["policy_version"],
                                      "devices": ", ".join(sorted(self._drive))})

    async def _after_control(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def _write_devices(self) -> list[tuple[str, str, int]]:
        """Declare the new Assets and their targets. Returns what to wait for."""
        written = []
        for name, a in self._assets.items():
            if a["spec"] is not None:
                out = await self._api.put(self._mid, "assets", name, a["spec"])
                written.append(("Asset", name, out["version"]))
        reqs = [a["requirement"] for a in self._assets.values() if a["requirement"]]
        if reqs:
            policies = {n: d for n, d in self._declared("ConstraintPolicy").items()
                        if (d["spec"].get("targetRef") or {}) == {"kind": "Site",
                                                                  "name": self._site}}
            name, cur = next(iter(policies.items()), (f"{self._site}-targets", None))
            out = await self._api.put(self._mid, "policies", name,
                                      targets_spec(self._site, reqs, cur["spec"] if cur else None),
                                      if_match=cur["version"] if cur else None)
            written.append(("ConstraintPolicy", name, out["version"]))
        return written

    async def _write_control(self) -> None:
        for asset in sorted(self._drive):
            await self._api.set_control(self._mid, "consent", True, asset, self._accepted)

    async def _wait_applied(self, written: list[tuple[str, str, int]]) -> None:
        """Until Ohmie applied each one; raises on a refusal. A slow apply is not an error."""
        for _ in range(APPLY_TRIES):
            got = {(o["kind"], o["name"]): o for o in await self._api.status(self._mid)}
            refused = [f"{k} {n}: {o['message']}" for k, n, v in written
                       if (o := got.get((k, n))) and o["version"] >= v
                       and o["status"] == "rejected"]
            if refused:
                raise OhmieApiError(422, "Rejected", refused)
            if all((o := got.get((k, n))) and o["version"] >= v for k, n, v in written):
                return
            await asyncio.sleep(APPLY_POLL)


PLAN_KINDS = {"spot": "Spot: the wholesale price, plus my retailer's margin",
              "schedule": "Time of use: peak, shoulder and off-peak rates",
              "flat": "One price, all day",
              "later": "I'll add it later"}
EXPORT_KINDS = {"none": "I am not paid for exports", "flat": "One buyback rate",
                "schedule": "Buyback rates that change through the day",
                "spot": "The spot price, less my retailer's margin"}
MAX_WINDOWS = 10   # peak, shoulder and off-peak bands, weekday and weekend
CONTROLLED_TYPES = {"hotwater", "flexload", "ev"}


def _window_schema(add_default: bool) -> dict[Any, Any]:
    return {vol.Optional("name"): selector.TextSelector(),
            vol.Required("rate_c"): _number(0, 200, 0.01, "c/kWh"),
            vol.Required("from"): selector.TimeSelector(),
            vol.Required("to"): selector.TimeSelector(),
            vol.Required("days", default="all"): _select(
                [("all", "Every day"), ("weekdays", "Weekdays"), ("weekends", "Weekends")]),
            vol.Required("add_another", default=add_default): selector.BooleanSelector()}


class PlanSteps:
    """"Your power plan" (ADR D10): what the member pays and is paid, read off the bill, from a
    date that may be in the past (a change of retailer recorded late). Shared by the wizard and the
    options flow. Needs self._api, self._mid, self._site, self._decls; fills self._plan with one
    SupplyContract spec per key: import, export, controlled."""

    _api: OhmieApi
    _mid: str
    _site: str
    _decls: list[dict[str, Any]]
    _plan: dict[str, dict[str, Any]]
    _plan_answers: dict[str, Any]
    _plan_jobs: list[tuple[str, str]]
    _plan_job: tuple[str, str] | None
    _plan_draft: dict[str, Any]

    def _today(self) -> str:
        return dt_util.now().date().isoformat()

    def _site_contracts(self) -> list[dict[str, Any]]:
        return [d for d in self._decls
                if d["kind"] == "SupplyContract" and d["spec"].get("siteRef") == self._site]

    def _active_contracts(self) -> dict[str, dict[str, Any]]:
        """The Site's contract in force today, per key (import, export, controlled)."""
        today, out = self._today(), {}
        for d in self._site_contracts():
            sp = d["spec"]
            if sp["validFrom"] <= today and (not sp.get("validTo") or sp["validTo"] > today):
                out[_key(sp)] = d
        return out

    def _controllable(self) -> list[str]:
        declared = [d["name"] for d in self._decls if d["kind"] == "Asset"
                    and d["spec"].get("siteRef") == self._site
                    and d["spec"].get("deviceType") in CONTROLLED_TYPES]
        new = [n for n, a in getattr(self, "_assets", {}).items()
               if a["binding"]["device_type"] in CONTROLLED_TYPES]
        return sorted(set(declared) | set(new))

    async def _after_plan(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_plan(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            since = str(user_input.get("since") or self._today())[:10]
            if since > self._today():
                errors["since"] = "since_future"
            else:
                self._plan_answers = {**user_input, "since": since}
                self._plan, self._plan_job = {}, None
                self._plan_jobs = [("import", user_input["kind"])] if user_input[
                    "kind"] != "later" else []
                if user_input.get("export_kind", "none") != "none":
                    self._plan_jobs.append(("export", user_input["export_kind"]))
                if user_input.get("controlled"):
                    self._plan_jobs.append(("controlled", ""))
                return await self._next_plan_job()
        schema: dict[Any, Any] = {
            vol.Required("kind", default="spot"): _select(list(PLAN_KINDS.items())),
            vol.Optional("retailer"): selector.TextSelector(),
            vol.Optional("daily_charge"): _number(0, 20, 0.01, "$/day"),
            vol.Required("export_kind", default="none"): _select(list(EXPORT_KINDS.items())),
            vol.Required("since", default=self._today()): selector.DateSelector()}
        if self._controllable():
            schema[vol.Required("controlled", default=False)] = selector.BooleanSelector()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="plan", data_schema=vol.Schema(schema), errors=errors)

    async def _next_plan_job(self) -> ConfigFlowResult:
        if not self._plan_jobs:
            return await self._after_plan()
        self._plan_job = self._plan_jobs.pop(0)
        self._plan_draft = {}
        key, kind = self._plan_job
        if key == "controlled":
            return await self.async_step_plan_controlled()
        return await getattr(self, f"async_step_plan_{kind}")()

    async def _finish_job(self, answers: dict[str, Any]) -> ConfigFlowResult:
        assert self._plan_job is not None
        key = self._plan_job[0]
        self._plan[key] = supply_spec(self._site, key, self._plan_answers["since"], {
            **self._plan_answers, **self._plan_draft, **answers})
        return await self._next_plan_job()

    def _flow_words(self) -> dict[str, str]:
        key = self._plan_job[0] if self._plan_job else "import"
        return {"flow": {"import": "what you pay", "export": "what you are paid for exports",
                         "controlled": "your controlled register"}[key]}

    async def async_step_plan_controlled(self, user_input: dict[str, Any] | None = None
                                         ) -> ConfigFlowResult:
        if user_input is not None:
            self._plan_draft = {"assets": user_input["assets"],
                                "guaranteed_hours": user_input.get("guaranteed_hours")}
            self._plan_job = ("controlled", user_input["controlled_kind"])
            return await getattr(self, f"async_step_plan_{user_input['controlled_kind']}")()
        devices = self._controllable()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="plan_controlled", data_schema=vol.Schema({
                vol.Required("assets", default=devices[:1]): _select(
                    [(d, d) for d in devices], multiple=True),
                vol.Required("controlled_kind", default="flat"): _select(
                    [("flat", PLAN_KINDS["flat"]), ("schedule", PLAN_KINDS["schedule"])]),
                vol.Optional("guaranteed_hours", default=17): _number(1, 24, 1, "h/day")}))

    async def async_step_plan_spot(self, user_input: dict[str, Any] | None = None
                                   ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._finish_job({"kind": "spot", **user_input})
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="plan_spot", description_placeholders=self._flow_words(),
            data_schema=vol.Schema({
                vol.Optional("margin_c"): _number(-10, 50, 0.01, "c/kWh"),
                vol.Optional("margin_pct"): _number(-50, 100, 0.1, "%")}))

    async def async_step_plan_flat(self, user_input: dict[str, Any] | None = None
                                   ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._finish_job({"kind": "flat", **user_input})
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="plan_flat", description_placeholders=self._flow_words(),
            data_schema=vol.Schema({vol.Required("rate_c"): _number(0, 200, 0.01, "c/kWh")}))

    async def async_step_plan_schedule(self, user_input: dict[str, Any] | None = None
                                       ) -> ConfigFlowResult:
        if user_input is not None:
            self._plan_draft["other_c"] = user_input["other_c"]
            self._plan_draft["windows"] = []
            return await self.async_step_plan_window()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="plan_schedule", description_placeholders=self._flow_words(),
            data_schema=vol.Schema({vol.Required("other_c"): _number(0, 200, 0.01, "c/kWh")}))

    async def async_step_plan_window(self, user_input: dict[str, Any] | None = None
                                     ) -> ConfigFlowResult:
        windows: list[dict[str, Any]] = self._plan_draft["windows"]
        errors: dict[str, str] = {}
        if user_input is not None:
            w = {"name": user_input.get("name"), "rate_c": user_input["rate_c"],
                 "from": user_input["from"][:5], "to": user_input["to"][:5],
                 "days": user_input.get("days") or "all"}
            if w["from"] == w["to"]:
                errors["base"] = "empty_window"
            else:
                windows.append(w)
                if user_input.get("add_another") and len(windows) < MAX_WINDOWS:
                    return self._show_window({})
                return await self._finish_job({"kind": "schedule"})
        return self._show_window(errors)

    def _show_window(self, errors: dict[str, str]) -> ConfigFlowResult:
        n = len(self._plan_draft["windows"]) + 1
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="plan_window", errors=errors,
            data_schema=vol.Schema(_window_schema(add_default=n < MAX_WINDOWS and n < 3)),
            description_placeholders={**self._flow_words(), "n": str(n),
                                      "max": str(MAX_WINDOWS)})

    async def _write_plan(self) -> list[tuple[str, str, int]]:
        """The new contracts from their date, which may be in the past. Each contract on the same
        key that runs past that date is cut there (its history stays) or, if it starts on or after
        it, replaced."""
        written: list[tuple[str, str, int]] = []
        since = self._plan_answers.get("since") or self._today()
        for key, spec in self._plan.items():
            name = supply_name(self._site, key, since)
            same_name = None
            for old in self._site_contracts():
                sp = old["spec"]
                if _key(sp) != key or (sp.get("validTo") and sp["validTo"] <= since):
                    continue
                if sp["validFrom"] < since:
                    out = await self._api.put(self._mid, "supply", old["name"],
                                              {**sp, "validTo": since}, if_match=old["version"])
                    written.append(("SupplyContract", old["name"], out["version"]))
                elif old["name"] == name:
                    same_name = old  # rewritten in place below
                else:
                    await self._api.delete(self._mid, "supply", old["name"])
            out = await self._api.put(self._mid, "supply", name, spec,
                                      if_match=same_name["version"] if same_name else None)
            written.append(("SupplyContract", name, out["version"]))
        return written


def _key(spec: dict[str, Any]) -> str:
    """import, export, or controlled: what one contract prices."""
    return "controlled" if spec.get("register") == "controlled" else spec["flow"]


class OhmieFlowHandler(PlanSteps, DeviceSteps,
                       config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._oauth: dict[str, Any] = {}
        self._members: list[dict[str, Any]] = []
        self._mid, self._role, self._site = "", "owner", ""
        self._site_phases, self._new_site, self._site_spec_v = "1P", False, None
        self._site_draft: dict[str, Any] | None = None
        self._icp: str | None = None
        self._node: str | None = None
        self._located: dict[str, Any] | None = None
        self._decls, self._catalog = [], {}
        self._style: str | None = None
        self._apply_task: asyncio.Task[None] | None = None
        self._apply_error = ""
        self._plan, self._plan_answers = {}, {}
        self._plan_jobs, self._plan_job, self._plan_draft = [], None, {}
        self._init_devices()

    @property
    def logger(self) -> logging.Logger:
        return _LOG

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": SCOPES}

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return OhmieOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        oauth.async_register(self.hass)
        return await super().async_step_user(user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None
                                        ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def _token(self) -> str:
        tok = self._oauth["token"]
        if float(tok.get("expires_at", 0)) < time.time() + 60:  # a long wizard: refresh
            self._oauth["token"] = tok = await self.flow_impl.async_refresh_token(tok)
        return str(tok["access_token"])

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        self._oauth = data
        self._api = OhmieApi(async_get_clientsession(self.hass), self._token)
        try:
            self._members = await self._api.members()
        except OhmieNotInvited:
            ir.async_create_issue(self.hass, DOMAIN, "invite_pending", is_fixable=False,
                                  severity=ir.IssueSeverity.WARNING,
                                  translation_key="invite_pending")
            return self.async_abort(reason="invite_pending")
        except OhmieApiError as exc:
            _LOG.warning("Ohmie: %s", exc)
            return self.async_abort(reason="cannot_connect")
        ir.async_delete_issue(self.hass, DOMAIN, "invite_pending")
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if entry.data[CONF_MEMBER] not in {m["member"] for m in self._members}:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data_updates=data)
        try:
            self._catalog = await self._api.catalog()
        except OhmieApiError:
            return self.async_abort(reason="cannot_connect")
        if not self._members:
            return await self.async_step_new_home()
        return await self.async_step_home()

    # --- home: create one, or pick a member ---

    async def async_step_home(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        owns = any(m["role"] == "owner" for m in self._members)
        if user_input is not None:
            if user_input["home"] == NEW:
                return await self.async_step_new_home()
            m = next(m for m in self._members if m["member"] == user_input["home"])
            self._mid, self._role = m["member"], m["role"]
            return await self.async_step_site_pick()
        options = [(m["member"], f"Ohmie home {m['member']} ({m['role']})")
                   for m in self._members]
        if not owns:
            options.append((NEW, "Create my own Ohmie home"))
        if len(options) == 1:
            return await self.async_step_home({"home": options[0][0]})
        return self.async_show_form(step_id="home", data_schema=vol.Schema(
            {vol.Required("home", default=options[0][0]): _select(options)}))

    async def async_step_new_home(self, user_input: dict[str, Any] | None = None
                                  ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="new_home", data_schema=vol.Schema({}))
        try:
            ref = await self._api.create_member()
        except OhmieApiError as exc:
            _LOG.warning("Ohmie: %s", exc)
            return self.async_abort(reason="cannot_connect")
        self._mid, self._role = ref["member"], "owner"
        self._decls = []
        return await self.async_step_site()

    # --- site: pick one, or add one ---

    async def async_step_site_pick(self, user_input: dict[str, Any] | None = None
                                   ) -> ConfigFlowResult:
        if user_input is None:
            self._decls = await self._api.declarations(self._mid)
        configured = {e.unique_id for e in self._async_current_entries()}
        free = [n for n in self._declared("Site") if f"{self._mid}-{n}" not in configured]
        if user_input is not None or not free:
            choice = (user_input or {}).get("site", NEW)
            if choice == NEW:
                return await self.async_step_site()
            spec = self._declared("Site")[choice]["spec"]
            self._site, self._new_site = choice, False
            self._site_phases, self._node = spec.get("phaseTopology", "1P"), spec.get("gxpNode")
            return await self._start_devices()
        return self.async_show_form(step_id="site_pick", data_schema=vol.Schema({
            vol.Required("site", default=free[0]): _select(
                [(n, n) for n in free] + [(NEW, "Add another site")])}))

    async def async_step_site(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if self._located is None:
            try:
                self._located = await self._api.locate(self.hass.config.latitude,
                                                       self.hass.config.longitude)
            except OhmieApiError:
                self._located = {}
        taken = set(self._declared("Site"))
        if user_input is not None:
            name = dns_label(user_input["name"])
            icp = re.sub(r"\s", "", user_input.get("icp") or "").upper() or None
            node = user_input.get("node")
            island = next((n["island"] for n in self._nodes() if n["node"] == node),
                          user_input.get("island"))
            if not LABEL.match(name) or name in taken:
                errors["name"] = "invalid_name"
            elif icp is not None and not ICP.match(icp):
                errors["icp"] = "invalid_icp"
            elif island is None:
                errors["base"] = "no_island"
            else:
                self._site, self._new_site, self._icp, self._node = name, True, icp, node
                self._site_phases = user_input["phases"]
                self._site_draft = site_spec(
                    timezone=self.hass.config.time_zone, phases=self._site_phases,
                    breaker_a=float(user_input["breaker_a"]),
                    export_kw=float(user_input["export_kw"]), node=node, island=island)
                return await self._start_devices()
        nodes = self._nodes()
        schema: dict[Any, Any] = {
            vol.Required("name", default=unique_name(
                dns_label(self.hass.config.location_name or "home"), taken)):
                selector.TextSelector()}
        if nodes:
            schema[vol.Required("node", default=nodes[0]["node"])] = _select(
                [(n["node"], f"{n['node']} ({n['distance_km']} km)") for n in nodes])
        else:
            schema[vol.Required("island")] = _select([("NI", "North Island"),
                                                      ("SI", "South Island")])
        schema.update({
            vol.Optional("icp"): selector.TextSelector(),
            vol.Required("phases", default="1P"): _select([("1P", "Single phase"),
                                                           ("3P", "Three phase")]),
            vol.Required("breaker_a", default=63): _number(10, 400, 1, "A"),
            vol.Required("export_kw", default=5): _number(0, 100, 0.5, "kW"),
        })
        return self.async_show_form(step_id="site", data_schema=vol.Schema(schema),
                                    errors=errors)

    def _nodes(self) -> list[dict[str, Any]]:
        loc = self._located or {}
        return ([{k: loc[k] for k in ("node", "island", "distance_km")}] if loc.get("node")
                else []) + list(loc.get("alternatives") or [])

    # --- devices ---

    async def _start_devices(self) -> ConfigFlowResult:
        await self.async_set_unique_id(f"{self._mid}-{self._site}")
        self._abort_if_unique_id_configured()
        return await self.async_step_devices()

    async def async_step_devices(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        if user_input is None:
            return await self._show_devices(set(), "devices")
        self._queue_devices(user_input, set())
        return await self._next_device()

    async def _after_devices(self) -> ConfigFlowResult:
        return await self.async_step_style()

    async def async_step_style(self, user_input: dict[str, Any] | None = None
                               ) -> ConfigFlowResult:
        if self._role == "viewer":
            return await self.async_step_control()
        if user_input is not None:
            self._style = user_input["style"]
            if self._active_contracts():  # a plan is in force: change it in the options
                return await self.async_step_control()
            return await self.async_step_plan()
        return self.async_show_form(step_id="style", data_schema=vol.Schema({
            vol.Required("style", default="balanced"): _select(
                [(k, STYLE_WORDS[k]) for k in STYLES])}))

    async def _after_plan(self) -> ConfigFlowResult:
        return await self.async_step_control()

    async def _after_control(self) -> ConfigFlowResult:
        return await self.async_step_summary()

    async def async_step_summary(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_apply()
        devices = ", ".join(f"{n} ({a['binding']['device_type']})"
                            for n, a in self._assets.items()) or "none yet"
        plan = self._plan.get("import")
        return self.async_show_form(step_id="summary", data_schema=vol.Schema({}),
                                    description_placeholders={
                                        "plan": PLAN_KINDS[plan["pricing"]["kind"]] if plan
                                        else "none yet",
                                        "site": self._site, "devices": devices,
                                        "style": self._style or "unchanged",
                                        "drive": ", ".join(sorted(self._drive)) or "none"})

    async def _apply(self) -> None:
        written: list[tuple[str, str, int]] = []
        if self._new_site:
            assert self._site_draft is not None
            out = await self._api.put(self._mid, "sites", self._site, self._site_draft,
                                      icp=self._icp)
            written.append(("Site", self._site, out["version"]))
        written += await self._write_devices()
        if self._style:
            prefs = {n: d for n, d in self._declared("PreferenceMode").items()
                     if (d["spec"].get("targetRef") or {}).get("name") == self._site}
            name, cur = next(iter(prefs.items()), (f"{self._site}-mode", None))
            spec = {**(cur["spec"] if cur else {}), **preference_spec(self._site, self._style)}
            out = await self._api.put(self._mid, "preferences", name, spec,
                                      if_match=cur["version"] if cur else None)
            written.append(("PreferenceMode", name, out["version"]))
        written += await self._write_plan()
        await self._write_control()
        await self._wait_applied(written)

    async def async_step_apply(self, user_input: dict[str, Any] | None = None
                               ) -> ConfigFlowResult:
        if self._apply_task is None:
            self._apply_task = self.hass.async_create_task(self._apply())
        if not self._apply_task.done():
            return self.async_show_progress(step_id="apply", progress_action="apply",
                                            progress_task=self._apply_task)
        try:
            self._apply_task.result()
        except OhmieApiError as exc:
            self._apply_error = "; ".join(exc.messages) or exc.reason
            return self.async_show_progress_done(next_step_id="apply_failed")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_apply_failed(self, user_input: dict[str, Any] | None = None
                                      ) -> ConfigFlowResult:
        return self.async_abort(reason="rejected",
                                description_placeholders={"problems": self._apply_error})

    async def async_step_finish(self, user_input: dict[str, Any] | None = None
                                ) -> ConfigFlowResult:
        return self.async_create_entry(title=f"Ohmie · {self._site}", data={
            **self._oauth, CONF_MEMBER: self._mid, CONF_SITE: self._site, CONF_ROLE: self._role,
            CONF_NODE: self._node, CONF_TERMS: self._accepted,
            CONF_BINDINGS: {n: a["binding"] for n, a in self._assets.items()}})


class OhmieOptionsFlow(PlanSteps, DeviceSteps, OptionsFlow):
    """Add devices, connect different entities, remove a device, or change control."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        rt = self.config_entry.runtime_data
        self._api, self._mid, self._site = rt.api, rt.member, rt.site
        self._role = rt.role
        self._site_phases = (rt.decls.get(("Site", rt.site)) or {}).get("spec", {}).get(
            "phaseTopology", "1P")
        self._decls = [{"kind": k, "name": n, **d} for (k, n), d in rt.decls.items()]
        self._init_devices()
        self._catalog = await self._api.catalog()
        menu = ["devices", "change", "entities", "power_plan", "remove", "balancing"] + (
            ["control"] if rt.role == "owner" else [])
        self._remap = False
        self._plan, self._plan_answers = {}, {}
        self._plan_jobs, self._plan_job, self._plan_draft = [], None, {}
        return self.async_show_menu(step_id="init", menu_options=menu)

    def _bound(self) -> dict[str, Any]:
        return dict(self.config_entry.data[CONF_BINDINGS])

    async def async_step_devices(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        if user_input is None:
            return await self._show_devices(set(self._bound()), "devices")
        self._queue_devices(user_input, set(self._bound()))
        return await self._next_device()

    async def _after_devices(self) -> ConfigFlowResult:
        if self._remap:  # the entities live in this entry only: Ohmie is not told
            self.hass.config_entries.async_update_entry(self.config_entry, data={
                **self.config_entry.data, CONF_BINDINGS: {
                    **self._bound(), **{n: a["binding"] for n, a in self._assets.items()}}})
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(data={})
        return await self.async_step_control()

    async def async_step_power_plan(self, user_input: dict[str, Any] | None = None
                                    ) -> ConfigFlowResult:
        return await self.async_step_plan()

    async def _after_plan(self) -> ConfigFlowResult:
        try:
            await self._wait_applied(await self._write_plan())
        except OhmieApiError as exc:
            return self.async_abort(reason="rejected", description_placeholders={
                "problems": "; ".join(exc.messages) or exc.reason})
        await self.config_entry.runtime_data.refresh_declarations()
        return self.async_create_entry(data={})

    async def async_step_entities(self, user_input: dict[str, Any] | None = None
                                  ) -> ConfigFlowResult:
        """Connect different entities to a device. Nothing changes in Ohmie."""
        bound = self._bound()
        if user_input is not None:
            b = bound[user_input["asset"]]
            self._remap = True
            self._queue = [{"bind": user_input["asset"], "device_type": b["device_type"],
                            "entities": dict(b["entities"]),
                            "signs": dict((b.get("options") or {}).get("signs") or {}),
                            "options": dict(b.get("options") or {}),
                            "label": user_input["asset"]}]
            return await self._next_device()
        return self.async_show_form(step_id="entities", data_schema=vol.Schema({
            vol.Required("asset"): _select([(a, a) for a in bound])}))

    async def _after_control(self) -> ConfigFlowResult:
        try:
            written = await self._write_devices()
            await self._write_control()
            await self._wait_applied(written)
        except OhmieApiError as exc:
            return self.async_abort(reason="rejected", description_placeholders={
                "problems": "; ".join(exc.messages) or exc.reason})
        data = {**self.config_entry.data,
                CONF_BINDINGS: {**self._bound(), **{n: a["binding"]
                                                    for n, a in self._assets.items()}}}
        if self._accepted:
            data[CONF_TERMS] = self._accepted
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_create_entry(data={})

    async def async_step_control(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        if not self._assets:  # from the menu: every bound device that can be driven
            rt = self.config_entry.runtime_data
            self._assets = {n: {"spec": None, "requirement": None, "binding": b}
                            for n, b in self._bound().items()}
            self._drive = {a for a, c in (rt.control.consent.items() if rt.control else [])
                           if c.granted}
            if user_input is None:
                return await super().async_step_control()
        if user_input is not None:
            rt = self.config_entry.runtime_data
            self._drive = set(user_input.get("drive") or [])
            for asset in set(self._driveable()) - self._drive:  # revokes need no terms
                if rt.control and (c := rt.control.consent.get(asset)) and c.granted:
                    await self._api.set_control(self._mid, "consent", False, asset)
            new = {a for a in self._drive if not (rt.control and (
                c := rt.control.consent.get(a)) and c.granted)}
            self._drive = new
            if new:
                return await self.async_step_consent()
            return await self._after_control()
        return await super().async_step_control()

    async def async_step_change(self, user_input: dict[str, Any] | None = None
                                ) -> ConfigFlowResult:
        """Correct an existing device's specs (capacity, power, reserve, run hours, peak)."""
        rt = self.config_entry.runtime_data
        changeable = sorted(n for (k, n), d in rt.decls.items()
                            if k == "Asset" and d["spec"].get("siteRef") == self._site
                            and change_fields(d["spec"]))
        if not changeable:
            return self.async_abort(reason="nothing_to_change")
        if user_input is not None:
            self._change = user_input["asset"]
            return await self.async_step_change_device()
        return self.async_show_form(step_id="change", data_schema=vol.Schema({
            vol.Required("asset"): _select([(a, a) for a in changeable])}))

    async def async_step_change_device(self, user_input: dict[str, Any] | None = None
                                       ) -> ConfigFlowResult:
        d = self.config_entry.runtime_data.decls[("Asset", self._change)]
        if user_input is not None:
            try:
                out = await self._api.put(self._mid, "assets", self._change,
                                          changed_spec(d["spec"], user_input),
                                          if_match=d["version"])
                await self._wait_applied([("Asset", self._change, out["version"])])
            except OhmieApiError as exc:
                return self.async_abort(reason="rejected", description_placeholders={
                    "problems": "; ".join(exc.messages) or exc.reason})
            await self.config_entry.runtime_data.refresh_declarations()
            return self.async_create_entry(data={})
        ranges = {"capacity_kwh": (0.5, 500, 0.1, "kWh"), "power_kw": (0.05, 100, 0.05, "kW"),
                  "reserve_pct": (0, 100, 5, "%"), "run_hours": (0.5, 24, 0.5, "h"),
                  "every_days": (1, 60, 1, "d"), "peak_kw": (0.1, 200, 0.1, "kW")}
        schema = {vol.Required(k, default=v if v is not None else ranges[k][0]): _number(*ranges[k])
                  for k, v in change_fields(d["spec"]).items()}
        return self.async_show_form(step_id="change_device", data_schema=vol.Schema(schema),
                                    description_placeholders={"device": self._change})

    async def async_step_balancing(self, user_input: dict[str, Any] | None = None
                                   ) -> ConfigFlowResult:
        """Load balancing at the edge (ADR D11). In Home Assistant: what reads each phase at the
        grid connection (A, or W and kW), its voltage, and each of the Site's circuits. In Ohmie,
        for every edge of the Site (Site.loadBalancing): how much of each limit Ohmie may commit,
        where it gives the power back (at most 15 points lower), and the voltage sag it acts on."""
        phases = {"1P": ["L1"], "2P": ["L1", "L2"], "3P": ["L1", "L2", "L3"]}.get(
            self._site_phases, ["L1"])
        data = self.config_entry.data
        site = self.config_entry.runtime_data.decls.get(("Site", self._site)) or {}
        spec = site.get("spec") or {}
        circuits = [c["name"] for c in spec.get("circuits") or []]
        lb = spec.get("loadBalancing") or {}
        commit = int(lb.get("commitPct", 90))
        release = int(lb.get("releasePct") or max(commit - 15, 30))
        sag = float(lb.get("voltageSagPct", 10))
        errors: dict[str, str] = {}
        if user_input is not None:
            pct, rel = int(user_input["commit_pct"]), int(user_input["release_pct"])
            if not (rel < pct and pct - rel <= 15):
                errors["release_pct"] = "release_gap"
            else:
                wanted = {"commitPct": pct, "releasePct": rel,
                          "voltageSagPct": user_input["sag_pct"]}
                if site and wanted != {**{"commitPct": commit, "releasePct": release,
                                          "voltageSagPct": sag}, **lb}:
                    try:
                        out = await self._api.put(self._mid, "sites", self._site,
                                                  {**spec, "loadBalancing": wanted},
                                                  if_match=site["version"])
                        await self._wait_applied([("Site", self._site, out["version"])])
                    except OhmieApiError as exc:
                        return self.async_abort(reason="rejected", description_placeholders={
                            "problems": "; ".join(exc.messages) or exc.reason})
                pick = lambda keys, prefix="": {k: user_input[f"{prefix}{k}"] for k in keys  # noqa: E731
                                                if user_input.get(f"{prefix}{k}")}
                self.hass.config_entries.async_update_entry(self.config_entry, data={
                    **data, CONF_BALANCE: pick(phases),
                    CONF_BALANCE_VOLTAGE: pick(phases, "voltage_"),
                    CONF_BALANCE_CIRCUITS: pick(circuits, "circuit_")})
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                return self.async_create_entry(data={})

        def sensor(key: str, now: str | None) -> tuple[Any, Any]:
            return (vol.Optional(key, description={"suggested_value": now}),
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")))

        schema: dict[Any, Any] = {}
        for p in phases:
            schema.update([sensor(p, (data.get(CONF_BALANCE) or {}).get(p))])
            schema.update([sensor(f"voltage_{p}", (data.get(CONF_BALANCE_VOLTAGE) or {}).get(p))])
        for c in circuits:
            schema.update([sensor(f"circuit_{c}", (data.get(CONF_BALANCE_CIRCUITS) or {}).get(c))])
        schema[vol.Required("commit_pct", default=commit)] = _number(30, 100, 5, "%")
        schema[vol.Required("release_pct", default=release)] = _number(30, 99, 1, "%")
        schema[vol.Required("sag_pct", default=sag)] = _number(2, 15, 0.5, "%")
        return self.async_show_form(step_id="balancing", data_schema=vol.Schema(schema),
                                    errors=errors)

    async def async_step_remove(self, user_input: dict[str, Any] | None = None
                                ) -> ConfigFlowResult:
        bound = self._bound()
        if user_input is not None:
            for asset in user_input.get("assets") or []:
                await self._api.delete(self._mid, "assets", asset)
                bound.pop(asset, None)
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_BINDINGS: bound})
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="remove", data_schema=vol.Schema({
            vol.Optional("assets", default=[]): _select([(a, a) for a in bound],
                                                        multiple=True)}))
