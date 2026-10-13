"""The Ohmie edge protocol, version 1 (foundation-documents api-specs/edge-protocol.md): subjects,
the messages Ohmie sends, and the hands-off rule. No Home Assistant imports: unit-tested on its
own."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

PERIOD = timedelta(minutes=30)
VERSION = 1
STATE_STREAM = "EDGE_STATE"


def inbox_prefix(subject: str) -> str:
    """The private inbox prefix the auth callout grants: _INBOX_ + base32(sha256(sub))[:16]."""
    digest = hashlib.sha256(subject.encode()).digest()
    return "_INBOX_" + base64.b32encode(digest).decode().rstrip("=")[:16]


def tel_subject(mid: str, asset: str) -> str:
    return f"ohmie.m.{mid}.tel.{asset}"


def ack_subject(mid: str, asset: str) -> str:
    return f"ohmie.m.{mid}.ack.{asset}"


def sig_subject(mid: str, asset: str) -> str:
    return f"ohmie.m.{mid}.sig.{asset}"


def budget_subject(mid: str, site: str) -> str:
    """The Site's load-balancing budget (ADR D11), kept last-value in EDGE_STATE."""
    return f"ohmie.m.{mid}.cfg.budget.{site}"


def ctl_subject(mid: str) -> str:
    return f"ohmie.m.{mid}.ctl.member"


def live_subjects(mid: str) -> list[str]:
    return [f"ohmie.m.{mid}.sig.*", f"ohmie.m.{mid}.ctl.*", f"ohmie.m.{mid}.cfg.>"]


def _time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Period:
    start: datetime
    setpoint_kw: float
    action: str  # charge | hold | discharge, or run | idle
    reason: str | None = None

    @property
    def end(self) -> datetime:
        return self.start + PERIOD

    @property
    def runs(self) -> bool:
        return self.action in ("charge", "run")


@dataclass(frozen=True)
class Plan:
    asset: str
    schedule_uuid: str
    computed_at: datetime
    effective_delivery: str  # advisory | hands-off
    control_reason: str | None
    periods: tuple[Period, ...]

    def period_at(self, now: datetime) -> Period | None:
        return next((p for p in self.periods if p.start <= now < p.end), None)

    def next_change(self, now: datetime) -> datetime | None:
        """The start of the first later period whose action differs from the current one."""
        cur = self.period_at(now)
        if cur is None:
            return None
        return next((p.start for p in self.periods if p.start > cur.start
                     and p.action != cur.action), None)

    def windows(self) -> list[tuple[datetime, datetime, str]]:
        """Runs of consecutive charge/run periods: (start, end, action)."""
        out: list[tuple[datetime, datetime, str]] = []
        for p in self.periods:
            if not p.runs:
                continue
            if out and out[-1][1] == p.start and out[-1][2] == p.action:
                out[-1] = (out[-1][0], p.end, p.action)
            else:
                out.append((p.start, p.end, p.action))
        return out


@dataclass(frozen=True)
class Consent:
    granted: bool
    policy_version: str
    at: datetime


@dataclass(frozen=True)
class Control:
    kill_switch: bool = False
    consent: dict[str, Consent] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    kind: str
    name: str
    version: int
    status: str  # applied | rejected
    problems: tuple[dict[str, Any], ...] = ()


class ProtocolError(ValueError):
    """A message this edge cannot read: a newer version, or a wrong shape."""


def _envelope(msg: dict[str, Any], kind: str) -> None:
    if msg.get("v") != VERSION or msg.get("type") != kind:
        raise ProtocolError(f"expected a v{VERSION} {kind}, got v{msg.get('v')} "
                            f"{msg.get('type')}")


def parse_plan(msg: dict[str, Any]) -> Plan:
    _envelope(msg, "plan")
    try:
        return Plan(asset=msg["asset"], schedule_uuid=msg["schedule_uuid"],
                    computed_at=_time(msg["computed_at"]),
                    effective_delivery=msg["effective_delivery"],
                    control_reason=msg.get("control_reason"),
                    periods=tuple(sorted((Period(_time(p["start"]), float(p["setpoint_kw"]),
                                                 p["action"], p.get("reason"))
                                          for p in msg["periods"]), key=lambda p: p.start)))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"bad plan: {exc}") from exc


def parse_control(msg: dict[str, Any]) -> Control:
    _envelope(msg, "control")
    try:
        return Control(kill_switch=bool(msg["kill_switch"]), consent={
            a: Consent(bool(c["granted"]), c["policy_version"], _time(c["at"]))
            for a, c in (msg.get("consent") or {}).items()})
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"bad control: {exc}") from exc


def parse_config(msg: dict[str, Any]) -> Config:
    _envelope(msg, "config")
    try:
        return Config(kind=msg["kind"], name=msg["name"], version=int(msg["version"]),
                      status=msg["status"], problems=tuple(msg.get("problems") or ()))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"bad config: {exc}") from exc


def plans_from_signals(rows: list[dict[str, Any]]) -> dict[str, Plan]:
    """The REST fallback (GET /v1/me/signals) as plans. Its rows are already masked with the kill
    switch and consent, so effective_delivery is the masked value."""
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(r)
    out = {}
    for asset, rs in by_asset.items():
        newest = max(rs, key=lambda r: r["computed_at"])
        out[asset] = Plan(asset=asset, schedule_uuid=newest["schedule_uuid"],
                          computed_at=_time(newest["computed_at"]),
                          effective_delivery=newest["effective_delivery"], control_reason=None,
                          periods=tuple(sorted((Period(_time(r["start"]), float(r["setpoint_kw"]),
                                                       r["action"], r.get("reason"))
                                                for r in rs), key=lambda p: p.start)))
    return out


def hands_off(plan: Plan | None, control: Control | None, asset: str,
              local_pause: bool) -> bool:
    """The edge may drive the device only when the plan, the kill switch and the consent all agree,
    and the member has not paused Ohmie here."""
    if plan is None or control is None or local_pause or control.kill_switch:
        return False
    consent = control.consent.get(asset)
    return plan.effective_delivery == "hands-off" and consent is not None and consent.granted
