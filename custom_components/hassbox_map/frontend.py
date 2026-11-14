from __future__ import annotations

import logging
import os
from pathlib import Path

from homeassistant.components.frontend import async_remove_panel
from homeassistant.core import HomeAssistant

from .const import PANEL_ICON, PANEL_TITLE, PANEL_URL_PATH, STATIC_URL_PATH

_LOGGER = logging.getLogger(__name__)

WEBCOMPONENT_NAME = "hb-map-panel"
CARD_URL_PATH = f"{STATIC_URL_PATH}/card.js"

def _frontend_dir() -> tuple[str, bool]:

    if dev_path := os.getenv("HB_MAP_FRONTEND_DIR"):
        return dev_path, True
    return str(Path(__file__).parent / "www"), False

async def async_register_frontend(hass: HomeAssistant, version: str) -> None:

    static_path, dev_mode = _frontend_dir()
    if dev_mode:
        _LOGGER.warning(
            "<HassBox Map Frontend> Frontend development mode enabled. Do not run in production!"
        )
    await _async_register_static_path(hass, static_path)
    await _async_register_card(hass, version)
    await async_register_panel(hass, version)

async def _async_register_card(hass: HomeAssistant, version: str) -> None:

    url = f"{CARD_URL_PATH}?v={version}"
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import (
            ResourceStorageCollection,
        )

        lovelace = hass.data.get(LOVELACE_DATA)
        resources = getattr(lovelace, "resources", None)
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_load()
            for item in resources.async_items():
                if str(item.get("url", "")).split("?")[0] == CARD_URL_PATH:

                    if item.get("url") != url:
                        await resources.async_update_item(item["id"], {"url": url})
                    return
            await resources.async_create_item({"res_type": "module", "url": url})
            return
    except Exception:
        _LOGGER.warning(
            "[hassbox_map] 注册卡片资源失败，回退到全局注入", exc_info=True
        )

    from homeassistant.components.frontend import add_extra_js_url

    add_extra_js_url(hass, url)

async def _async_register_static_path(hass: HomeAssistant, static_path: str) -> None:

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL_PATH, static_path, False)]
        )
    except ImportError:
        hass.http.register_static_path(
            STATIC_URL_PATH, static_path, cache_headers=False
        )

async def async_register_panel(hass: HomeAssistant, version: str) -> None:

    from homeassistant.components import panel_custom

    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        async_remove_panel(hass, PANEL_URL_PATH)

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=WEBCOMPONENT_NAME,
        module_url=f"{STATIC_URL_PATH}/panel.js?v={version}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
    )
