"""The base entity: one Home Assistant device per Ohmie Asset ("Ohmie · <asset>"), and one for the
Site. Entities update when the runtime says something changed; nothing polls."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .runtime import OhmieRuntime


class OhmieEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, rt: OhmieRuntime, asset: str | None, key: str) -> None:
        self.rt, self.asset = rt, asset
        site_id = f"{rt.member}-{rt.site}"
        self._attr_translation_key = key
        self._attr_unique_id = f"{site_id}-{asset or 'site'}-{key}"
        if asset is None:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, site_id)}, name=f"Ohmie · {rt.site}", manufacturer="Ohmie",
                model="Site", configuration_url="https://app.ohmie.energy")
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{site_id}-{asset}")}, name=f"Ohmie · {asset}",
                manufacturer="Ohmie", model=rt.assets.get(asset, "device"))
            if rt.site_device_id:
                self._attr_device_info["via_device_id"] = rt.site_device_id

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.rt.async_add_listener(self.async_write_ha_state))
