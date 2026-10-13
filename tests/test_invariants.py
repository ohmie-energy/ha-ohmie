"""Rules the code must keep, checked on the source itself."""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).parents[1] / "custom_components" / "ohmie"


def test_ohmie_never_calls_a_service_on_the_members_devices():
    # Neutral control (ADR D9, 8 Oct 2026): Ohmie changes only its own entities. The member's own
    # automations, behind their own conditions, decide what reaches real equipment.
    calls = [f"{p.name}:{i}" for p in sorted(SRC.glob("*.py"))
             for i, line in enumerate(p.read_text().splitlines(), 1)
             if re.search(r"services\.async_call|async_call_service|\.call_service\(", line)]
    assert calls == []
