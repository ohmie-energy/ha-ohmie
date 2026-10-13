"""The edge protocol helpers: no Home Assistant needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ohmie.edge import (
    Consent,
    Control,
    ProtocolError,
    hands_off,
    inbox_prefix,
    parse_config,
    parse_control,
    parse_plan,
    plans_from_signals,
)

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def plan_msg(actions=("charge", "charge", "hold", "charge"), delivery="hands-off", **kw):
    return {"v": 1, "type": "plan", "ts": T0.isoformat(), "asset": "battery",
            "schedule_uuid": "u1", "computed_at": "2026-10-08T08:31:02Z",
            "effective_delivery": delivery, "control_reason": "Consented", "extra": "ignored",
            "periods": [{"start": (T0 + timedelta(minutes=30 * i)).isoformat(),
                         "setpoint_kw": 3.0 if a == "charge" else 0.0, "action": a,
                         "reason": "cheap"} for i, a in enumerate(actions)], **kw}


def test_inbox_prefix_matches_the_auth_callout():
    # The same value as InboxPrefix("300000000000000001") in apps/ohmie-nats-auth/perms.go.
    assert inbox_prefix("300000000000000001") == "_INBOX_TLI5MDOIYGLG62LR"


def test_a_plan_gives_the_current_period_the_next_change_and_its_windows():
    plan = parse_plan(plan_msg())
    now = T0 + timedelta(minutes=10)
    assert plan.period_at(now).action == "charge"
    assert plan.next_change(now) == T0 + timedelta(minutes=60)
    assert plan.windows() == [(T0, T0 + timedelta(hours=1), "charge"),
                              (T0 + timedelta(minutes=90), T0 + timedelta(hours=2), "charge")]
    assert plan.period_at(T0 + timedelta(hours=3)) is None


@pytest.mark.parametrize("msg", [{**plan_msg(), "v": 2}, {**plan_msg(), "type": "control"},
                                 {**plan_msg(), "periods": [{"start": "x"}]}])
def test_an_unreadable_plan_is_refused(msg):
    with pytest.raises(ProtocolError):
        parse_plan(msg)


def test_control_and_config_parse():
    ctl = parse_control({"v": 1, "type": "control", "ts": "x", "kill_switch": False,
                         "consent": {"battery": {"granted": True, "policy_version": "consent-v0",
                                                 "at": "2026-10-08T08:00:00Z"}}})
    assert ctl.consent["battery"].granted
    cfg = parse_config({"v": 1, "type": "config", "ts": "x", "kind": "asset", "name": "pump",
                        "version": 3, "status": "rejected",
                        "problems": [{"reason": "DanglingRef", "message": "no site"}]})
    assert cfg.status == "rejected" and cfg.problems[0]["reason"] == "DanglingRef"


GRANT = Control(consent={"battery": Consent(True, "consent-v0", T0)})


@pytest.mark.parametrize(("delivery", "control", "pause", "expected"), [
    ("hands-off", GRANT, False, True),
    ("advisory", GRANT, False, False),
    ("hands-off", Control(kill_switch=True, consent=GRANT.consent), False, False),
    ("hands-off", Control(), False, False),                     # no consent for this device
    ("hands-off", Control(consent={"battery": Consent(False, "consent-v0", T0)}), False, False),
    ("hands-off", GRANT, True, False),                          # the local pause
    ("hands-off", None, False, False),                          # no control message yet
])
def test_hands_off_needs_the_plan_the_kill_switch_the_consent_and_no_pause(
        delivery, control, pause, expected):
    plan = parse_plan(plan_msg(delivery=delivery))
    assert hands_off(plan, control, "battery", pause) is expected


def test_rest_signals_become_plans():
    rows = [{"asset": "pump", "start": (T0 + timedelta(minutes=30 * i)).isoformat(),
             "setpoint_kw": 0.8, "action": "run", "reason": None, "effective_delivery": "advisory",
             "computed_at": "2026-10-08T08:00:00Z", "schedule_uuid": "s"} for i in (1, 0)]
    plan = plans_from_signals(rows)["pump"]
    assert [p.start for p in plan.periods] == [T0, T0 + timedelta(minutes=30)]
    assert plan.windows() == [(T0, T0 + timedelta(hours=1), "run")]
