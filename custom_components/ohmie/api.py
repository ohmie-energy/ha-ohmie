"""The Ohmie REST API (api.ohmie.energy, openapi.json in ohmie-energy/gitops apps/ohmie-api): setup,
declarations, control, and the fallback for the edge protocol when NATS is down."""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import API_URL

TIMEOUT = aiohttp.ClientTimeout(total=20)


class OhmieApiError(Exception):
    def __init__(self, status: int, reason: str, messages: list[str] | None = None) -> None:
        super().__init__(f"{status} {reason}: {'; '.join(messages or [])}".rstrip(": "))
        self.status, self.reason, self.messages = status, reason, messages or []


class OhmieAuthError(OhmieApiError):
    """The token is not accepted (401): sign in again."""


class OhmieNotInvited(OhmieApiError):
    """The account has no `pro` role yet (403 on a member route): the invite is pending."""


def token_claims(token: str) -> dict[str, Any]:
    """The JWT payload, NOT verified: the edge only reads its own `sub` and `exp`. The server
    verifies every token."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class OhmieApi:
    def __init__(self, session: aiohttp.ClientSession, token: Callable[[], Awaitable[str]],
                 base: str = API_URL) -> None:
        self._session, self._token, self._base = session, token, base

    async def _req(self, method: str, path: str, *, body: Any = None, auth: bool = True,
                   headers: dict[str, str] | None = None) -> Any:
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = f"Bearer {await self._token()}"
        try:
            async with self._session.request(method, self._base + path, json=body, headers=hdrs,
                                             timeout=TIMEOUT) as resp:
                text, status = await resp.text(), resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OhmieApiError(0, "Unreachable", [str(exc) or type(exc).__name__]) from exc
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if status < 400:
            return data
        err = data if isinstance(data, dict) else {}
        reason = err.get("reason") or "Error"
        messages = err.get("messages") or ([str(err["detail"])] if "detail" in err else [])
        if status == 401:
            raise OhmieAuthError(status, "Unauthorized", messages)
        if status == 403 and "tier" in " ".join(messages):
            raise OhmieNotInvited(status, "NotInvited", messages)
        raise OhmieApiError(status, reason, messages)

    # setup
    async def catalog(self) -> dict[str, Any]:
        return await self._req("GET", "/v1/catalog", auth=False)

    async def consent(self) -> dict[str, Any]:
        return await self._req("GET", "/v1/consent/current", auth=False)

    async def locate(self, lat: float, lon: float) -> dict[str, Any]:
        return await self._req("POST", "/v1/locate", body={"lat": lat, "lon": lon})

    # members and declarations
    async def members(self) -> list[dict[str, Any]]:
        return (await self._req("GET", "/v1/members"))["members"]

    async def create_member(self) -> dict[str, Any]:
        return await self._req("POST", "/v1/members")

    async def declarations(self, mid: str) -> list[dict[str, Any]]:
        return (await self._req("GET", f"/v1/members/{mid}/declarations"))["declarations"]

    async def status(self, mid: str) -> list[dict[str, Any]]:
        return (await self._req("GET", f"/v1/members/{mid}/status"))["objects"]

    async def put(self, mid: str, segment: str, name: str, spec: dict[str, Any], *,
                  icp: str | None = None, if_match: int | str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"spec": spec}
        if icp is not None:
            body["icp"] = icp
        headers = {"If-Match": f'"{if_match}"'} if if_match is not None else None
        return await self._req("PUT", f"/v1/members/{mid}/{segment}/{name}", body=body,
                               headers=headers)

    async def delete(self, mid: str, segment: str, name: str) -> dict[str, Any]:
        return await self._req("DELETE", f"/v1/members/{mid}/{segment}/{name}",
                               headers={"If-Match": "*"})

    # control (the owner only)
    async def control(self, mid: str) -> dict[str, Any]:
        return await self._req("GET", f"/v1/members/{mid}/control")

    async def set_control(self, mid: str, kind: str, value: bool, asset: str | None = None,
                          policy_version: str | None = None) -> dict[str, Any]:
        return await self._req("POST", f"/v1/members/{mid}/control", body={
            "kind": kind, "asset": asset, "value": value, "policy_version": policy_version})

    # the edge, over REST (the fallback)
    async def devices(self) -> dict[str, Any]:
        return await self._req("GET", "/v1/me/devices")

    async def signals(self, hours: int = 24) -> list[dict[str, Any]]:
        return (await self._req("GET", f"/v1/me/signals?hours={hours}"))["signals"]

    async def telemetry(self, readings: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._req("POST", "/v1/me/telemetry", body={"readings": readings})

    async def site_price(self, mid: str, site: str, hours: int = 24) -> list[dict[str, Any]]:
        """The Site's price: its power plan, else its ICP's total. 404 when Ohmie has neither."""
        return (await self._req("GET", f"/v1/members/{mid}/sites/{site}/price?hours={hours}")
                )["periods"]

    async def put_forecast(self, mid: str, asset: str, source: str,
                           periods: list[tuple[str, float]]) -> dict[str, Any]:
        """A device's forecast (today: PV production), 30-minute periods in kW."""
        return await self._req("PUT", f"/v1/members/{mid}/assets/{asset}/forecast", body={
            "source": source, "periods": [{"start": t, "power_kw": kw} for t, kw in periods]})

    async def site_budget(self, mid: str, site: str) -> dict[str, Any]:
        """The Site's load-balancing budget (ADR D11): the REST copy of cfg.budget.<site>."""
        return await self._req("GET", f"/v1/members/{mid}/sites/{site}/budget")

    async def site_report(self, mid: str, site: str, weeks: int = 2) -> dict[str, Any]:
        """Per local week: paid on the plan, truthful spot, and what Ohmie's plans would save."""
        return await self._req("GET", f"/v1/members/{mid}/sites/{site}/report?weeks={weeks}")

    async def supply_events(self, mid: str) -> list[dict[str, Any]]:
        """Supply cuts Ohmie found on the member's controlled registers, newest first."""
        return (await self._req("GET", f"/v1/members/{mid}/supply-events"))["events"]

    async def answer_supply_event(self, mid: str, event_id: str,
                                  confirmed: bool) -> dict[str, Any]:
        return await self._req("POST", f"/v1/members/{mid}/supply-events/{event_id}",
                               body={"confirmed": confirmed})

    async def forecast(self, node: str, horizon: str = "24h") -> list[dict[str, Any]]:
        return (await self._req("GET", f"/v1/forecast/{node}?horizon={horizon}"))["periods"]
