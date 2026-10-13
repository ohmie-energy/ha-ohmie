"""One config entry (one Ohmie Site) at run time: the NATS link, the plans and control state it
receives, the commands and Ohmie switches, telemetry out, the REST fallback, and the repairs.
Entities read from here and register a listener for changes. Ohmie calls no service on the member's
devices: it changes only its own entities (ADR D9, neutral control)."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import OhmieApi, OhmieApiError
from .balance import Balancer, Budget, Decision, headroom, parse_budget
from .commands import Command, Window, capped, command, next_window, output_on
from .const import (
    BALANCE_EVERY,
    CONF_BALANCE,
    CONF_BALANCE_CIRCUITS,
    CONF_BALANCE_VOLTAGE,
    CONF_BINDINGS,
    CONF_MEMBER,
    CONF_NODE,
    CONF_ROLE,
    CONF_SITE,
    CONF_TERMS,
    DECISION_EVENT,
    DOMAIN,
    FORECAST_EVERY,
    NATS_URL,
    REPORT_EVERY,
    REST_AFTER,
    REST_POLL_EVERY,
    SAMPLE_EVERY,
    SEND_EVERY,
    SOLAR_EVERY,
    SUPPLY_EVERY,
)
from .edge import (
    PERIOD,
    Consent,
    Control,
    Plan,
    ProtocolError,
    ack_subject,
    budget_subject,
    ctl_subject,
    hands_off,
    live_subjects,
    parse_config,
    parse_control,
    parse_plan,
    plans_from_signals,
    sig_subject,
    tel_subject,
)
from .model import Ack, Binding, EntityState, period_floor
from .nats_link import NatsLink
from .solar import energy_dashboard_forecasts, periods_from_wh_hours
from .telemetry import Batch, Batcher, Queue, reading

_LOG = logging.getLogger(__name__)
SEGMENT = {"Site": "sites", "Asset": "assets", "ConstraintPolicy": "policies",
           "PreferenceMode": "preferences"}
STALE_PLAN = timedelta(hours=1)
DRIVEN = {"battery", "ev", "hotwater", "flexload"}  # the device types Ohmie plans and commands
UNAVAILABLE_FOR = timedelta(minutes=15)


OUTPUT_TYPES = {"hotwater", "flexload", "ev"}       # on/off devices: each gets an Ohmie switch
NORMAL_STATE = {"hotwater": "on", "flexload": "off", "ev": "on"}


def binding_from(asset: str, raw: dict[str, Any]) -> Binding:
    return Binding(asset, raw["device_type"], dict(raw.get("entities") or {}),
                   dict(raw.get("options") or {}))


class OhmieRuntime:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: OhmieApi,
                 token: Callable[[], Awaitable[str]], link_factory: Any = None) -> None:
        self.hass, self.entry, self.api = hass, entry, api
        self.member: str = entry.data[CONF_MEMBER]
        self.site: str = entry.data[CONF_SITE]
        self.role: str = entry.data.get(CONF_ROLE, "owner")
        self.node: str | None = entry.data.get(CONF_NODE)
        self.bindings = {a: binding_from(a, b) for a, b in entry.data[CONF_BINDINGS].items()}
        self.assets: dict[str, str] = {a: b.device_type for a, b in self.bindings.items()}
        self.decls: dict[tuple[str, str], dict[str, Any]] = {}
        self.plans: dict[str, Plan] = {}
        self.control: Control | None = None
        self.forecast: list[dict[str, Any]] = []   # the nodal forecast (p50, the cheap label)
        self.signal: list[dict[str, Any]] = []     # the Site price: its plan, else the total
        self.consent_version: str | None = None
        self.site_device_id: str | None = None   # the Site's HA device; Asset devices hang off it
        self.local_pause = False
        self.paused_until: dict[str, datetime] = {}  # a reported override, until the next period
        self.latest: dict[str, dict[str, Any]] = {}
        self.manual: dict[str, tuple[bool, datetime]] = {}  # an Ohmie switch set by hand: until
        self.tel_queue, self.ack_queue, self.batcher = Queue(), Queue(), Batcher()
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._unavailable_since: dict[str, datetime] = {}
        self._supply_asked: set[str] = set()
        self.weekly_report: dict[str, Any] | None = None
        self.phase_entities: dict[str, str] = dict(entry.data.get(CONF_BALANCE) or {})
        self.circuit_entities: dict[str, str] = dict(entry.data.get(CONF_BALANCE_CIRCUITS) or {})
        self.voltage_entities: dict[str, str] = dict(entry.data.get(CONF_BALANCE_VOLTAGE) or {})
        self.balancer = Balancer()                   # load balancing (balance.py)
        self._last_rest_poll = 0.0
        self._lock = asyncio.Lock()
        self.link = (link_factory or NatsLink)(NATS_URL, live_subjects(self.member), token,
                                               self._on_message, self._on_connect,
                                               self._on_link_state)

    # --- life cycle ---

    async def async_start(self) -> None:
        saved = await self._store.async_load() or {}
        self.local_pause = bool(saved.get("pause"))
        self.tel_queue = Queue.from_list(saved.get("telemetry") or [])
        self.ack_queue = Queue.from_list(saved.get("acks") or [])
        await self.refresh_declarations(reload=False)
        await self._refresh_forecast()
        await self._refresh_report()
        if self.phase_entities:
            try:
                self.balancer.budget = parse_budget(
                    await self.api.site_budget(self.member, self.site))
            except (OhmieApiError, KeyError, TypeError, ValueError) as exc:
                _LOG.debug("budget: %s", exc)
        try:
            self.consent_version = (await self.api.consent())["policy_version"]
        except OhmieApiError as exc:
            _LOG.debug("consent version: %s", exc)
        await self.link.start()
        self._unsubs += [
            async_track_time_interval(self.hass, self._tick, SAMPLE_EVERY),
            async_track_time_interval(self.hass, self._send, SEND_EVERY),
            async_track_time_interval(self.hass, self._forecast_tick, FORECAST_EVERY),
            async_track_time_interval(self.hass, self._solar_tick, SOLAR_EVERY),
            async_track_time_interval(self.hass, self._supply_tick, SUPPLY_EVERY),
            async_track_time_interval(self.hass, self._report_tick, REPORT_EVERY),
        ]
        if self.phase_entities:
            self._unsubs += [
                async_track_state_change_event(
                    self.hass, [*self.phase_entities.values(), *self.circuit_entities.values(),
                                *self.voltage_entities.values()], self._on_phase),
                async_track_time_interval(self.hass, self._balance_tick, BALANCE_EVERY),
            ]
            self._balance_now()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.link.stop()
        for b in self.batcher.flush(dt_util.utcnow()):
            self.tel_queue.add(b)
        await self._store.async_save(self._saved())

    def _saved(self) -> dict[str, Any]:
        return {"pause": self.local_pause, "telemetry": self.tel_queue.to_list(),
                "acks": self.ack_queue.to_list()}

    def _save(self) -> None:
        self._store.async_delay_save(self._saved, 10)

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # --- what entities read ---

    def mode(self, asset: str) -> str:
        return "hands-off" if hands_off(self.plans.get(asset), self.control, asset,
                                        self.local_pause) else "advisory"

    @property
    def paused(self) -> bool:
        return self.local_pause or bool(self.control and self.control.kill_switch)

    @property
    def rest_mode(self) -> bool:
        down = self.link.down_since
        return down is not None and time.monotonic() - down >= REST_AFTER.total_seconds()

    @property
    def connected(self) -> bool:
        """NATS (or the REST fallback) is up and a current plan is held."""
        now = dt_util.utcnow()
        held = any(p.period_at(now) for p in self.plans.values()) or not self.assets
        return (self.link.connected or self.rest_mode) and held

    @property
    def driven(self) -> list[str]:
        """The Site's devices that Ohmie plans: each gets a command, and consent to follow it."""
        return sorted(a for a, t in self.assets.items() if t in DRIVEN)

    def _planned_command(self, asset: str) -> Command:
        return command(self.plans.get(asset), self.control, asset, self.local_pause,
                       self.paused_until.get(asset), dt_util.utcnow())

    def command(self, asset: str) -> Command:
        """Ohmie's command for a device, under the load-balancing cap when one is set."""
        return capped(self._planned_command(asset), self.caps.get(asset))

    # --- load balancing at the edge (ADR D11) ---

    def _number(self, entity: str) -> tuple[float, str] | None:
        st = self.hass.states.get(entity)
        try:
            return (float(st.state), st.attributes.get("unit_of_measurement", "")) if st else None
        except ValueError:
            return None

    def _amps(self, entity: str, phases: int = 1) -> float | None:
        """A current (A), or a power (W, kW) at the budget's voltage spread over `phases`."""
        got = self._number(entity)
        if got is None:
            return None
        volts = self.budget.volts if self.budget else 230.0
        factor = {"A": 1.0, "mA": 0.001, "W": 1 / (volts * phases),
                  "kW": 1000 / (volts * phases)}.get(got[1] or "A")
        return None if factor is None else abs(got[0]) * factor

    def phase_loads(self) -> dict[str, float]:
        """Amps per phase at the grid connection. A phase whose entity has no number is left out."""
        return {p: a for p, e in self.phase_entities.items() if (a := self._amps(e)) is not None}

    def circuit_loads(self) -> dict[str, float]:
        """Amps per phase on each circuit with an entity (a power is spread over its phases)."""
        phases = {c.name: len(c.phases) for c in (self.budget.circuits if self.budget else ())}
        return {c: a for c, e in self.circuit_entities.items()
                if (a := self._amps(e, phases.get(c, 1))) is not None}

    def voltages(self) -> dict[str, float]:
        out = {}
        for p, e in self.voltage_entities.items():
            got = self._number(e)
            factor = {"V": 1.0, "kV": 1000.0, "mV": 0.001}.get(got[1] or "V") if got else None
            if got is not None and factor is not None:
                out[p] = got[0] * factor
        return out

    def headroom(self) -> dict[str, float]:
        return headroom(self.budget, self.phase_loads()) if self.budget else {}

    @property
    def budget(self) -> Budget | None:
        return self.balancer.budget

    @property
    def caps(self) -> dict[str, float]:
        return self.balancer.caps

    @callback
    def _balance_now(self) -> None:
        """One balancing step: at most one action, then the engine waits to see its result."""
        if self.balancer.budget is None:
            return
        wanted = {a: c.power_kw for a in self.driven
                  if (c := self._planned_command(a)).state in ("charge", "run")}
        before = dict(self.balancer.caps)
        now = dt_util.utcnow()
        decision = self.balancer.step(self.phase_loads(), wanted, now,
                                      circuit_a=self.circuit_loads(), voltage=self.voltages())
        if decision:
            self._record(decision, now)
        if decision or self.balancer.caps != before:
            self._notify()

    def _record(self, decision: Decision, now: datetime) -> None:
        """Why the engine acted: in Home Assistant's Logbook, and to Ohmie as an ack (its history,
        GET /v1/members/<id>/history), so the member can read it back later."""
        _LOG.info("load balancing: %s", decision.text)
        self.hass.bus.async_fire(DECISION_EVENT, {"asset": decision.asset,
                                                  "outcome": decision.outcome,
                                                  "text": decision.text})
        plan = self.plans.get(decision.asset)
        period = plan.period_at(now) if plan else None
        setpoint = decision.cap_kw if decision.cap_kw is not None else (
            period.setpoint_kw if period else None)
        self._queue_ack(decision.asset, Ack(decision.outcome, period_floor(now),
                                            plan.schedule_uuid if plan else None, setpoint,
                                            decision.text), now)

    @callback
    def _on_phase(self, event: Any) -> None:
        self._balance_now()

    async def _balance_tick(self, now: datetime) -> None:
        self._balance_now()

    def _storage(self, asset: str) -> dict[str, Any]:
        return ((self.decls.get(("Asset", asset)) or {}).get("spec") or {}).get("storage") or {}

    def reserve(self, asset: str) -> float | None:
        return self._storage(asset).get("socMinPct")

    def window(self, asset: str, actions: set[str]) -> Window | None:
        return next_window(self.plans.get(asset), dt_util.utcnow(), actions,
                           (self.latest.get(asset) or {}).get("soc_pct"),
                           self._storage(asset).get("energyCapacityKwh"))

    def report(self, asset: str, outcome: str, detail: str | None) -> None:
        """A member's automation says what happened to a command (the `ohmie.report` action). An
        override hands the device back until the next period."""
        now = dt_util.utcnow()
        start = period_floor(now)
        if outcome == "overridden":
            self.paused_until[asset] = start + PERIOD
        plan = self.plans.get(asset)
        period = plan.period_at(now) if plan else None
        self._queue_ack(asset, Ack(outcome, start, plan.schedule_uuid if plan else None,
                                   period.setpoint_kw if period else None, detail), now)
        self._notify()

    def output(self, asset: str) -> bool:
        """The Ohmie switch of an on/off device. Set by hand, it keeps that to the period end."""
        manual = self.manual.get(asset)
        if manual and dt_util.utcnow() < manual[1]:
            return manual[0]
        b = self.bindings.get(asset)
        normal = ((b.options.get("normal_state") if b else None)
                  or NORMAL_STATE.get(self.assets.get(asset, ""), "off"))
        return output_on(self.command(asset), normal)

    def set_output(self, asset: str, on: bool) -> None:
        """The member switched the Ohmie switch by hand: it wins until the next period. Ohmie
        records an override only when it differed from Ohmie's command."""
        if on == self.output(asset):
            return
        now = dt_util.utcnow()
        commanded = self.command(asset).state != "normal"
        self.manual[asset] = (on, period_floor(now) + PERIOD)
        if commanded:
            self.report(asset, "overridden",
                        f"the Ohmie switch was turned {'on' if on else 'off'} by hand")
        self._notify()

    @property
    def labelled(self) -> list[dict[str, Any]]:
        """The periods that say what is cheap: a ToU plan's own windows when the member has one,
        else the market's label on the nodal forecast."""
        return self.signal if any(r.get("label") for r in self.signal) else self.forecast

    def requirement(self, asset: str) -> tuple[str, dict[str, Any]] | None:
        """(policy name, requirement) for the asset in this Site's ConstraintPolicies."""
        for (kind, name), d in self.decls.items():
            target = d["spec"].get("targetRef") or {}
            if kind == "ConstraintPolicy" and target.get("name") == self.site:
                for r in d["spec"].get("requirements") or []:
                    if r.get("assetRef") == asset:
                        return name, r
        return None

    # --- messages in ---

    async def _on_message(self, subject: str, data: dict[str, Any]) -> None:
        parts = subject.split(".")
        try:
            if parts[3] == "sig" and parts[4] in self.assets:
                self.plans[parts[4]] = parse_plan(data)
            elif parts[3] == "ctl":
                self.control = parse_control(data)
            elif parts[3] == "cfg" and parts[4] == "budget":
                if len(parts) > 5 and parts[5] == self.site:
                    self.balancer.budget = parse_budget(data)
                    self._balance_now()
            elif parts[3] == "cfg":
                cfg = parse_config(data)
                if cfg.status == "applied":
                    await self.refresh_declarations()
                else:
                    _LOG.warning("Ohmie refused %s %s: %s", cfg.kind, cfg.name, cfg.problems)
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            _LOG.warning("ignored a message on %s: %s", subject, exc)
            return
        self._notify()

    async def _on_connect(self) -> None:
        for subject in [ctl_subject(self.member), budget_subject(self.member, self.site)] + [
                sig_subject(self.member, a) for a in self.assets]:
            msg = await self.link.last(subject)
            if msg is not None:
                await self._on_message(subject, msg)
        self.hass.async_create_task(self._send(dt_util.utcnow()))

    @callback
    def _on_link_state(self, up: bool) -> None:
        self._notify()

    async def refresh_declarations(self, reload: bool = True) -> None:
        try:
            rows = await self.api.declarations(self.member)
        except OhmieApiError as exc:
            _LOG.debug("declarations: %s", exc)
            return
        self.decls = {(d["kind"], d["name"]): d for d in rows}
        site_assets = {n: d["spec"].get("deviceType") for (k, n), d in self.decls.items()
                       if k == "Asset" and d["spec"].get("siteRef") == self.site}
        if (reload and set(site_assets) - set(self.assets)
                and self.entry.state is ConfigEntryState.LOADED):
            # A device was added elsewhere (the app, an installer): reload to make its entities.
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
        self.assets.update(site_assets)
        self._notify()

    # --- Ohmie's own entities ---

    def _states(self, b: Binding) -> dict[str, EntityState]:
        out = {}
        for e in b.entities.values():
            st = self.hass.states.get(e)
            if st is not None:
                out[e] = EntityState(st.state, dict(st.attributes))
        return out

    def _queue_ack(self, asset: str, ack: Any, now: datetime) -> None:
        latest = self.latest.get(asset) or {}
        self.ack_queue.add(Batch(asset, ack.ack_id, {
            "v": 1, "type": "ack", "ts": now.isoformat(), "ack_id": ack.ack_id,
            "schedule_uuid": ack.schedule_uuid, "period_start": ack.period_start.isoformat(),
            "outcome": ack.outcome, "setpoint_kw": ack.setpoint_kw,
            "reading": {k: latest[k] for k in ("soc_pct", "power_kw") if k in latest},
            "detail": ack.detail}))
        self._save()

    # --- timers ---

    async def _tick(self, now: datetime) -> None:
        minute = now.replace(second=0, microsecond=0)
        for asset, b in self.bindings.items():
            r = reading(b, self._states(b), minute)
            if r is not None:
                self.batcher.add(asset, r)
                self.latest[asset] = r
        if self.rest_mode and time.monotonic() - self._last_rest_poll >= \
                REST_POLL_EVERY.total_seconds():
            await self._rest_poll()
        self._repairs(now)
        self._notify()

    async def _rest_poll(self) -> None:
        self._last_rest_poll = time.monotonic()
        try:
            plans = plans_from_signals(await self.api.signals())
            ctl = await self.api.control(self.member)
        except OhmieApiError as exc:
            _LOG.debug("REST fallback: %s", exc)
            return
        self.plans.update({a: p for a, p in plans.items() if a in self.assets})
        self.control = Control(kill_switch=ctl["kill_switch"], consent={
            a: Consent(c["granted"], c["policy_version"], dt_util.parse_datetime(c["at"])
                       or dt_util.utcnow()) for a, c in ctl["consent"].items()})

    async def _send(self, now: datetime) -> None:
        for b in self.batcher.flush(now):
            self.tel_queue.add(b)
        sent = 0
        while (head := self.tel_queue.peek()) is not None:
            if self.link.connected:
                ok = await self.link.publish(tel_subject(self.member, head.asset), head.payload,
                                             head.msg_id)
            elif self.rest_mode:
                try:
                    await self.api.telemetry([{**r, "asset": head.asset}
                                              for r in head.payload["readings"]])
                    ok = True
                except OhmieApiError:
                    ok = False
            else:
                ok = False
            if not ok:
                break
            self.tel_queue.done(head.msg_id)
            sent += 1
        while self.link.connected and (head := self.ack_queue.peek()) is not None:
            if not await self.link.publish(ack_subject(self.member, head.asset), head.payload,
                                           head.msg_id):
                break
            self.ack_queue.done(head.msg_id)
            sent += 1
        if sent:
            self._save()

    async def _solar_tick(self, now: datetime) -> None:
        """The Energy dashboard's solar forecast, to Ohmie, for the Site's PV device (ADR D10)."""
        pv = next((a for a, t in sorted(self.assets.items()) if t == "pv"), None)
        if pv is None or self.role == "viewer":
            return
        forecasts, source = await energy_dashboard_forecasts(self.hass)
        periods = periods_from_wh_hours(forecasts, now)
        if not periods:
            return
        try:
            await self.api.put_forecast(self.member, pv, source, periods)
        except OhmieApiError as exc:
            _LOG.debug("solar forecast: %s", exc)

    async def _report_tick(self, now: datetime) -> None:
        await self._refresh_report()
        self._notify()

    async def _refresh_report(self) -> None:
        try:
            self.weekly_report = await self.api.site_report(self.member, self.site)
        except OhmieApiError as exc:
            _LOG.debug("report: %s", exc)

    async def _supply_tick(self, now: datetime) -> None:
        """A repair per supply cut Ohmie suspects on a controlled register, for the member to answer
        (a writer only); a measured or answered cut asks nothing."""
        if self.role == "viewer":
            return
        try:
            events = await self.api.supply_events(self.member)
        except OhmieApiError as exc:
            _LOG.debug("supply events: %s", exc)
            return
        asking = set()
        for e in events:
            if e["status"] != "suspected":
                continue
            asking.add(e["event_id"])
            start = dt_util.as_local(dt_util.parse_datetime(e["started_at"], raise_on_error=True))
            end = dt_util.as_local(dt_util.parse_datetime(e["ended_at"], raise_on_error=True))
            ir.async_create_issue(
                self.hass, DOMAIN, f"{self.entry.entry_id}_supply_{e['event_id']}",
                is_fixable=True, severity=ir.IssueSeverity.WARNING, translation_key="supply_cut",
                translation_placeholders={
                    "asset": e["asset"], "start": start.strftime("%a %d %b %H:%M"),
                    "end": end.strftime("%H:%M"),
                    "notice": f" Transpower notice {e['notice_id']} was in force."
                              if e.get("notice_id") else ""},
                data={"entry_id": self.entry.entry_id, "event_id": e["event_id"]})
        for eid in self._supply_asked - asking:
            ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_supply_{eid}")
        self._supply_asked = asking

    async def answer_supply(self, event_id: str, confirmed: bool) -> None:
        """The member's answer to a suspected cut (the repair's fix flow)."""
        await self.api.answer_supply_event(self.member, event_id, confirmed)
        self._supply_asked.discard(event_id)
        ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_supply_{event_id}")

    async def _forecast_tick(self, now: datetime) -> None:
        await self._refresh_forecast()
        self._notify()

    async def _refresh_forecast(self) -> None:
        try:
            self.signal = await self.api.site_price(self.member, self.site)
        except OhmieApiError as exc:  # no plan and no ICP tariff yet (404): the nodal forecast
            self.signal = []
            _LOG.debug("site price: %s", exc)
        if not self.node:
            return
        try:
            self.forecast = await self.api.forecast(self.node)
        except OhmieApiError as exc:
            _LOG.debug("forecast: %s", exc)

    # --- repairs ---

    def _repairs(self, now: datetime) -> None:
        for asset in self.driven:
            c = self.control.consent.get(asset) if self.control else None
            if not (c and c.granted):
                self._issue(f"plan_stale_{asset}", False, "plan_stale", {"asset": asset})
                continue
            plan = self.plans.get(asset)
            fresh = plan is not None and (plan.period_at(now) is not None
                                          or now - plan.computed_at < STALE_PLAN)
            self._issue(f"plan_stale_{asset}", not fresh and self.link.connected,
                        "plan_stale", {"asset": asset})
        for b in self.bindings.values():
            for e in b.entities.values():
                st = self.hass.states.get(e)
                if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                    self._unavailable_since.setdefault(e, now)
                else:
                    self._unavailable_since.pop(e, None)
                since = self._unavailable_since.get(e)
                self._issue(f"entity_unavailable_{e}", since is not None
                            and now - since >= UNAVAILABLE_FOR, "entity_unavailable",
                            {"entity": e, "asset": b.asset})
        outdated = bool(self.control and self.consent_version and any(
            c.granted and c.policy_version != self.consent_version
            for c in self.control.consent.values()))
        self._issue("consent_outdated", outdated, "consent_outdated",
                    {"version": self.consent_version or ""})

    def _issue(self, issue_id: str, active: bool, key: str, placeholders: dict[str, str]) -> None:
        full = f"{self.entry.entry_id}_{issue_id}"
        if active:
            ir.async_create_issue(self.hass, DOMAIN, full, is_fixable=False,
                                  severity=ir.IssueSeverity.WARNING, translation_key=key,
                                  translation_placeholders=placeholders)
        else:
            ir.async_delete_issue(self.hass, DOMAIN, full)

    # --- member actions (entities call these) ---

    async def set_pause(self, on: bool) -> None:
        """Acts here at once, also offline; then records the kill switch (the owner only)."""
        self.local_pause = on
        self._save()
        self._notify()
        if self.role == "owner":
            try:
                await self.api.set_control(self.member, "kill_switch", on)
            except OhmieApiError as exc:
                _LOG.warning("the pause acts here, but Ohmie did not record it: %s", exc)

    async def set_follow(self, asset: str, on: bool) -> None:
        if self.role != "owner":
            raise HomeAssistantError("Only the member can let Ohmie drive a device")
        terms = self.entry.data.get(CONF_TERMS)
        if on and (terms is None or terms != self.consent_version):
            raise HomeAssistantError("Accept Ohmie's current terms first: open the Ohmie "
                                     "integration and choose Configure, then Control")
        try:
            out = await self.api.set_control(self.member, "consent", on, asset,
                                             terms if on else None)
        except OhmieApiError as exc:
            raise HomeAssistantError(f"Ohmie did not record the change: {exc}") from exc
        self.control = Control(kill_switch=out["kill_switch"], consent={
            a: Consent(c["granted"], c["policy_version"], dt_util.parse_datetime(c["at"])
                       or dt_util.utcnow()) for a, c in out["consent"].items()})
        self._notify()

    async def update_declaration(self, kind: str, name: str,
                                 change: Callable[[dict[str, Any]], None]) -> None:
        """Change one declared object: read, change, write over the version read (If-Match)."""
        for attempt in (1, 2):
            if attempt == 2:
                await self.refresh_declarations()
            current = self.decls.get((kind, name))
            if current is None:
                raise HomeAssistantError(f"{kind} {name} is not declared")
            spec = copy.deepcopy(current["spec"])
            change(spec)
            try:
                await self.api.put(self.member, SEGMENT[kind], name, spec,
                                   if_match=current["version"])
            except OhmieApiError as exc:
                if exc.status in (409, 412) and attempt == 1:
                    continue  # changed elsewhere since we read it: read again, once
                raise HomeAssistantError(f"Ohmie refused the change: {exc}") from exc
            current["spec"], current["version"] = spec, current["version"] + 1
            self._notify()
            return
