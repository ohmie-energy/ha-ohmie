"""The REST client against a mocked api.ohmie.energy."""

from __future__ import annotations

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ohmie.api import (
    OhmieApi,
    OhmieApiError,
    OhmieAuthError,
    OhmieNotInvited,
    token_claims,
)
from custom_components.ohmie.const import API_URL

from .conftest import fake_jwt


async def _token() -> str:
    return "tok"


@pytest.fixture
async def api(hass, aioclient_mock):
    return OhmieApi(async_get_clientsession(hass), _token)


async def test_requests_carry_the_bearer_token(api, aioclient_mock):
    aioclient_mock.get(f"{API_URL}/v1/members", json={"members": [
        {"member": "t35t01", "namespace": "member-t35t01", "role": "owner", "state": "active"}]})
    assert (await api.members())[0]["member"] == "t35t01"
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"


async def test_status_codes_map_to_errors(api, aioclient_mock):
    aioclient_mock.get(f"{API_URL}/v1/members", status=401, json={"detail": "expired"})
    with pytest.raises(OhmieAuthError):
        await api.members()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_URL}/v1/members", status=403,
                       json={"detail": "the member routes needs the pro tier; this token has free"})
    with pytest.raises(OhmieNotInvited):
        await api.members()


async def test_a_refused_declaration_says_why(api, aioclient_mock):
    aioclient_mock.put(f"{API_URL}/v1/members/t35t01/assets/pump", status=422,
                       json={"reason": "DanglingRef", "messages": ["site home is not declared"]})
    with pytest.raises(OhmieApiError) as err:
        await api.put("t35t01", "assets", "pump", {"siteRef": "home"})
    assert err.value.reason == "DanglingRef" and err.value.messages == [
        "site home is not declared"]


async def test_put_sends_if_match_and_the_icp_in_the_body(api, aioclient_mock):
    aioclient_mock.put(f"{API_URL}/v1/members/t35t01/sites/home", status=202,
                       json={"kind": "Site", "name": "home", "version": 3, "status": "pending"})
    out = await api.put("t35t01", "sites", "home", {"timezone": "Pacific/Auckland"},
                        icp="0001234567PC0F1", if_match=2)
    assert out["version"] == 3
    _, _, body, headers = aioclient_mock.mock_calls[0]
    assert body == {"spec": {"timezone": "Pacific/Auckland"}, "icp": "0001234567PC0F1"}
    assert headers["If-Match"] == '"2"'


async def test_public_routes_send_no_token(api, aioclient_mock):
    aioclient_mock.get(f"{API_URL}/v1/catalog", json={"version": 1})
    await api.catalog()
    assert "Authorization" not in (aioclient_mock.mock_calls[0][3] or {})


async def test_locate_posts_the_location_in_the_body(api, aioclient_mock):
    aioclient_mock.post(f"{API_URL}/v1/locate", json={"node": "MGM0331", "island": "NI",
                                                      "distance_km": 4.6, "alternatives": []})
    assert (await api.locate(-40.7, 175.6))["node"] == "MGM0331"
    method, url, body, _ = aioclient_mock.mock_calls[0]
    assert body == {"lat": -40.7, "lon": 175.6} and not url.query


def test_token_claims_reads_sub_and_exp_without_verifying():
    claims = token_claims(fake_jwt("abc"))
    assert claims["sub"] == "abc" and claims["exp"] > 0
