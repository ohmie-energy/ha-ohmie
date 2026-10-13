"""The NATS link against a real nats-server (WebSocket, JetStream), with the member's permissions
set exactly as the auth callout grants them (apps/ohmie-nats-auth perms.go). Skipped without a
server."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import AsyncIterator

import nats
import pytest
from nats.js.api import StreamConfig

from custom_components.ohmie import nats_link
from custom_components.ohmie.edge import inbox_prefix, live_subjects
from custom_components.ohmie.nats_link import NatsLink

from .conftest import fake_jwt

pytestmark = pytest.mark.nats
SERVER = os.environ.get("NATS_SERVER_BIN") or shutil.which("nats-server")
SUB, MID = "300000000000000001", "t35t01"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(port: int, ws: int, store: str) -> str:
    m = f"ohmie.m.{MID}"
    perms = {"publish": {"allow": [f"{m}.tel.*", f"{m}.ack.*",
                                   f"$JS.API.DIRECT.GET.EDGE_STATE.{m}.>"]},
             "subscribe": {"allow": [f"{inbox_prefix(SUB)}.>", f"{m}.sig.*", f"{m}.ctl.*",
                                     f"{m}.cfg.>"]}}
    return f"""
listen: 127.0.0.1:{port}
websocket {{ listen: "127.0.0.1:{ws}", no_tls: true }}
jetstream {{ store_dir: "{store}" }}
accounts {{
  OHMIE {{
    jetstream: enabled
    users: [
      {{ user: admin, password: admin }}
      {{ user: member, password: unused, permissions: {json.dumps(perms)} }}
    ]
  }}
}}
no_auth_user: member
"""


@pytest.fixture
async def server(socket_enabled) -> AsyncIterator[tuple[str, nats.NATS]]:
    if not SERVER:
        pytest.skip("no nats-server (set NATS_SERVER_BIN)")
    port, ws = _port(), _port()
    with tempfile.TemporaryDirectory() as tmp:
        conf = os.path.join(tmp, "nats.conf")
        with open(conf, "w") as f:
            f.write(_config(port, ws, os.path.join(tmp, "js")))
        proc = subprocess.Popen([SERVER, "-c", conf], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    admin = await nats.connect(f"nats://admin:admin@127.0.0.1:{port}")
                    break
                except Exception:  # noqa: BLE001 - starting up
                    await asyncio.sleep(0.1)
            js = admin.jetstream()
            await js.add_stream(StreamConfig(name="EDGE_TELEMETRY", subjects=["ohmie.m.*.tel.*"],
                                             duplicate_window=120))
            await js.add_stream(StreamConfig(name="EDGE_ACK", subjects=["ohmie.m.*.ack.*"]))
            await js.add_stream(StreamConfig(
                name="EDGE_STATE", subjects=["ohmie.m.*.sig.*", "ohmie.m.*.ctl.*",
                                             "ohmie.m.*.cfg.>"],
                max_msgs_per_subject=1, allow_direct=True))
            yield f"ws://127.0.0.1:{ws}", admin
            await admin.close()
        finally:
            proc.terminate()
            proc.wait(5)


class MemberLink(NatsLink):
    """The test server takes this connection as its no_auth_user, so no token is sent."""

    def _credential(self) -> str:
        return ""


class Edge:
    def __init__(self, url: str, exp_in: int = 1800) -> None:
        self.messages: list[tuple[str, dict]] = []
        self.connects = 0
        self.tokens = 0
        self.exp_in = exp_in

        async def token() -> str:
            self.tokens += 1
            return fake_jwt(SUB, self.exp_in)

        async def on_message(subject, data):
            self.messages.append((subject, data))

        async def on_connect():
            self.connects += 1

        self.link = MemberLink(url, live_subjects(MID), token, on_message, on_connect,
                             lambda up: None)


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_telemetry_is_stored_once_and_state_is_read_by_direct_get(server):
    url, admin = server
    edge = Edge(url)
    await edge.link.start()
    try:
        await _until(lambda: edge.link.connected)
        assert edge.connects == 1
        tel = {"v": 1, "type": "telemetry", "readings": [{"time": "t", "soc_pct": 50}]}
        assert await edge.link.publish(f"ohmie.m.{MID}.tel.battery", tel, "batch-1")
        assert await edge.link.publish(f"ohmie.m.{MID}.tel.battery", tel, "batch-1")  # a replay
        info = await admin.jetstream().stream_info("EDGE_TELEMETRY")
        assert info.state.messages == 1
        for n in (1, 2):
            await admin.jetstream().publish(f"ohmie.m.{MID}.sig.battery",
                                            json.dumps({"n": n}).encode())
        assert await edge.link.last(f"ohmie.m.{MID}.sig.battery") == {"n": 2}
        assert await edge.link.last(f"ohmie.m.{MID}.sig.ev") is None
    finally:
        await edge.link.stop()


async def test_live_messages_arrive_and_another_members_subjects_are_refused(server):
    url, admin = server
    edge = Edge(url)
    await edge.link.start()
    try:
        await _until(lambda: edge.link.connected)
        await admin.jetstream().publish(f"ohmie.m.{MID}.ctl.member",
                                        json.dumps({"type": "control"}).encode())
        await _until(lambda: edge.messages)
        assert edge.messages[0] == (f"ohmie.m.{MID}.ctl.member", {"type": "control"})
        assert not await edge.link.publish("ohmie.m.zzzzzz.tel.battery", {"v": 1}, "x")
        assert await edge.link.last("ohmie.m.zzzzzz.sig.battery") is None
        assert edge.link.connected  # a refused subject does not drop the link
    finally:
        await edge.link.stop()


async def test_the_link_renews_before_the_token_expires_and_after_a_close(server, monkeypatch):
    monkeypatch.setattr(nats_link, "RENEW_BEFORE", 1799.5)
    monkeypatch.setattr(nats_link, "MIN_LIFETIME", 0.3)
    url, _ = server
    edge = Edge(url)
    await edge.link.start()
    try:
        await _until(lambda: edge.connects >= 3)        # renewed, each with a fresh token
        assert edge.tokens >= 3 and edge.link.connected
        monkeypatch.setattr(nats_link, "MIN_LIFETIME", 30.0)
        before = edge.connects
        await _until(lambda: edge.connects > before)
        await edge.link._nc.close()                     # the server closes it (token expiry)
        await _until(lambda: edge.connects > before + 1 and edge.link.connected)
    finally:
        await edge.link.stop()
    assert not edge.link.connected
