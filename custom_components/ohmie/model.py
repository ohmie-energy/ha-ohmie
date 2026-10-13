"""Shared value types. No Home Assistant imports."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class EntityState:
    state: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Binding:
    """One Ohmie Asset and the member's entities that Ohmie READS for it (role -> entity id).
    Ohmie never calls a service on them."""
    asset: str
    device_type: str
    entities: Mapping[str, str]
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ack:
    outcome: str  # applied | overridden | failed
    period_start: datetime
    schedule_uuid: str | None
    setpoint_kw: float | None
    detail: str | None = None
    ack_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def period_floor(t: datetime) -> datetime:
    return t - timedelta(minutes=t.minute % 30, seconds=t.second, microseconds=t.microsecond)
