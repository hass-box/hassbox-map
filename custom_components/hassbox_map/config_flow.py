from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN
from .utils.store import async_load_from_store

STORE_CONFIG_KEY = "hassbox_map.config"

class HassBoxMapConfigFlow(ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if self.hass.data.get(DOMAIN):
            return self.async_abort(reason="single_instance_allowed")

        config = await async_load_from_store(self.hass, STORE_CONFIG_KEY) or {}
        return self.async_create_entry(
            title="HassBox 地图",
            data={
                "token": config.get("token", ""),
                "service_id": config.get("service_id", ""),
            },
        )
