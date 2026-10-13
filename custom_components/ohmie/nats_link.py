"""The NATS connection of one member edge (edge-protocol.md, "Connect").

The access token is the NATS credential, and the server closes the connection when it expires; it
does not reconnect by itself after that. So the link renews: some time before the token expires it
gets a fresh token, closes the connection and opens a new one. Network drops in between are
nats-py's own reconnects, which read the current token through a callback. After every new
connection the `on_connect` callback re-reads the last value of each state subject (direct get), so
a message sent while the link was down is never missed."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import nats
from nats.aio.client import Client
from nats.errors import Error as NatsError
from nats.js.errors import NotFoundError

from .api import token_claims
from .edge import STATE_STREAM, inbox_prefix

_LOG = logging.getLogger(__name__)
RENEW_BEFORE = 90.0        # seconds before the token (or the server's 60 min cap) runs out
MIN_LIFETIME = 30.0        # never renew sooner than this after a connect
SERVER_CAP = 3600.0        # the auth callout's longest grant
BACKOFF = (2, 5, 15, 30, 60, 120, 300)

OnMessage = Callable[[str, dict[str, Any]], Awaitable[None]]


class NatsLink:
    def __init__(self, url: str, subjects: list[str], token: Callable[[], Awaitable[str]],
                 on_message: OnMessage, on_connect: Callable[[], Awaitable[None]],
                 on_state: Callable[[bool], None]) -> None:
        self._url, self._subjects, self._get_token = url, subjects, token
        self._on_message, self._on_connect, self._on_state = on_message, on_connect, on_state
        self._nc: Client | None = None
        self._token = ""
        self._task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self.connected = False
        self.down_since: float | None = time.monotonic()

    def _set(self, up: bool) -> None:
        if up != self.connected:
            self.connected = up
            self.down_since = None if up else time.monotonic()
            self._on_state(up)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ohmie-nats-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close()

    async def _close(self) -> None:
        nc, self._nc = self._nc, None
        if nc is not None and not nc.is_closed:
            try:
                await nc.close()
            except NatsError:
                pass
        self._set(False)

    async def _connect(self) -> float:
        """Open a connection; returns when it must be renewed (monotonic seconds)."""
        self._token = await self._get_token()
        claims = token_claims(self._token)
        self._closed.clear()

        async def closed() -> None:
            self._closed.set()

        async def disconnected() -> None:
            self._set(False)

        async def reconnected() -> None:
            self._set(True)
            await self._on_connect()

        async def error(exc: Exception) -> None:
            _LOG.debug("nats: %s", exc)

        nc = await nats.connect(
            self._url, token=self._credential, inbox_prefix=inbox_prefix(claims["sub"]),
            name="ha-ohmie", ping_interval=30, max_outstanding_pings=3, connect_timeout=10,
            allow_reconnect=True, max_reconnect_attempts=-1, reconnect_time_wait=5,
            closed_cb=closed, disconnected_cb=disconnected, reconnected_cb=reconnected,
            error_cb=error)
        for subject in self._subjects:
            await nc.subscribe(subject, cb=self._handle)
        self._nc = nc
        self._set(True)
        await self._on_connect()
        lifetime = min(float(claims.get("exp", 0)) - time.time(), SERVER_CAP)
        return time.monotonic() + max(lifetime - RENEW_BEFORE, MIN_LIFETIME)

    def _credential(self) -> str:
        """The token nats-py sends on every (re)connect: always the newest one."""
        return self._token

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                renew_at = await self._connect()
                attempt = 0
                try:
                    await asyncio.wait_for(self._closed.wait(),
                                           timeout=max(renew_at - time.monotonic(), 1))
                    _LOG.info("nats: the connection closed; reconnecting with a fresh token")
                except TimeoutError:
                    _LOG.debug("nats: renewing the connection before the token expires")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any failure: back off and try again with a fresh token
                _LOG.warning("nats: cannot connect to %s: %s", self._url, exc)
                await self._close()
                await asyncio.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1
                continue
            await self._close()

    async def _handle(self, msg: Any) -> None:
        try:
            data = json.loads(msg.data)
        except ValueError:
            _LOG.warning("nats: %s is not JSON; ignored", msg.subject)
            return
        await self._on_message(msg.subject, data)

    async def last(self, subject: str) -> dict[str, Any] | None:
        """The last message on a state subject (a direct get in its subject form)."""
        if self._nc is None or not self.connected:
            return None
        try:
            msg = await self._nc.jetstream(timeout=5).get_last_msg(STATE_STREAM, subject,
                                                                    direct=True)
        except NotFoundError:
            return None
        except (NatsError, TimeoutError) as exc:  # a refused subject gets no reply at all
            _LOG.debug("nats: no last value for %s: %s", subject, exc)
            return None
        return json.loads(msg.data) if msg.data else None

    async def publish(self, subject: str, payload: dict[str, Any], msg_id: str) -> bool:
        """A JetStream publish that waits for the stream's ack. False when it was not stored."""
        if self._nc is None or not self.connected:
            return False
        try:
            await self._nc.jetstream(timeout=5).publish(
                subject, json.dumps(payload, separators=(",", ":")).encode(),
                headers={"Nats-Msg-Id": msg_id})
        except (NatsError, TimeoutError) as exc:
            _LOG.debug("nats: publish to %s failed: %s", subject, exc)
            return False
        return True
