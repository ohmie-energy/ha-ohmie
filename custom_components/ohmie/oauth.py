"""Sign-in with Ohmie (Zitadel at auth.ohmie.energy): the authorisation code flow with PKCE. The
client is public and built in, so the member types no credentials and nothing secret ships."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import AUTHORIZE_URL, CLIENT_ID, DOMAIN, TOKEN_URL


class OhmieImplementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    @property
    def name(self) -> str:
        return "Ohmie"


def async_register(hass: HomeAssistant) -> None:
    """Register (or replace) the implementation. A new one per sign-in: a fresh PKCE verifier."""
    config_entry_oauth2_flow.async_register_implementation(
        hass, DOMAIN, OhmieImplementation(hass, DOMAIN, CLIENT_ID, AUTHORIZE_URL, TOKEN_URL))
