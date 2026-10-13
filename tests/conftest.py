"""Shared fixtures. Tokens are built in memory; nothing secret is written to disk."""

from __future__ import annotations

import base64
import json
import time

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def fake_jwt(sub: str = "300000000000000001", exp_in: int = 1800) -> str:
    """A JWT-shaped access token. The edge reads `sub` and `exp` from it; the server verifies it."""
    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return ".".join([part({"alg": "RS256"}), part({"sub": sub, "exp": int(time.time()) + exp_in}),
                     "sig"])
