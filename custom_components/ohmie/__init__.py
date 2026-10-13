"""Ohmie for Home Assistant: price-aware plans for your devices, and (when you allow it) Ohmie
drives them. One config entry is one Ohmie Site. api-specs/home-assistant-contract.md is the
contract."""

from __future__ import annotations

import voluptuous as vol
from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import oauth
from .api import OhmieApi, OhmieApiError, OhmieAuthError, OhmieNotInvited
from .const import DOMAIN
from .runtime import OhmieRuntime

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.CALENDAR,
             Platform.NUMBER, Platform.TIME]

type OhmieConfigEntry = ConfigEntry[OhmieRuntime]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
REPORT_SCHEMA = vol.Schema({
    vol.Required("asset"): cv.string,
    vol.Required("outcome"): vol.In(["applied", "overridden", "failed"]),
    vol.Optional("detail"): vol.All(cv.string, vol.Length(max=500)),
    vol.Optional("site"): cv.string,
})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def report(call: ServiceCall) -> None:
        """A member's automation says what happened to Ohmie's command for one device."""
        asset, site = call.data["asset"], call.data.get("site")
        found = [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)
                 if asset in e.runtime_data.assets and site in (None, e.runtime_data.site)]
        if len(found) != 1:
            raise ServiceValidationError(
                f"no single Ohmie device {asset!r}" + ("" if site else "; give its site"))
        found[0].report(asset, call.data["outcome"], call.data.get("detail"))

    hass.services.async_register(DOMAIN, "report", report, schema=REPORT_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OhmieConfigEntry) -> bool:
    oauth.async_register(hass)
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry)
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def token() -> str:
        """A valid access token, refreshed when it is about to expire. A refused refresh means
        the member must sign in again."""
        try:
            await session.async_ensure_token_valid()
        except ClientResponseError as exc:
            if 400 <= exc.status < 500:
                entry.async_start_reauth(hass)
                raise ConfigEntryAuthFailed("Ohmie refused the sign-in; sign in again") from exc
            raise
        return str(session.token["access_token"])

    api = OhmieApi(async_get_clientsession(hass), token)
    try:
        await token()
        await api.members()
    except OhmieAuthError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except OhmieNotInvited as exc:  # the pro role was taken away: retry until it is back
        ir.async_create_issue(hass, DOMAIN, "invite_pending", is_fixable=False,
                              severity=ir.IssueSeverity.WARNING, translation_key="invite_pending")
        raise ConfigEntryNotReady("the Ohmie invite is pending") from exc
    except (OhmieApiError, ClientResponseError) as exc:
        raise ConfigEntryNotReady(f"Ohmie is not reachable: {exc}") from exc

    ir.async_delete_issue(hass, DOMAIN, "invite_pending")
    runtime = OhmieRuntime(hass, entry, api, token)
    runtime.site_device_id = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{runtime.member}-{runtime.site}")},
        name=f"Ohmie · {runtime.site}", manufacturer="Ohmie", model="Site").id
    await runtime.async_start()
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OhmieConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
