from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .bridge import BridgeManager
from .const import (
    BRIDGE_WS_URL,
    CONF_REPORT_LOCATION,
    DATA_BRIDGE,
    DATA_STORE,
    DATA_TRACKERS,
    DOMAIN,
    PANEL_URL_PATH,
)
from .data_client import HassBoxDataClient
from .frontend import async_register_frontend
from .reporter import LocationReporter
from .storage import DeviceStore, GeofenceStore
from .device_tracker import TrackerManager
from .websocket import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)

def _entry_value(entry: ConfigEntry, key: str):

    return entry.options.get(key, entry.data.get(key))

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    hass.data.setdefault(DOMAIN, {})

    store = GeofenceStore(hass)
    await store.async_load()

    trackers = TrackerManager(hass, entry)

    trackers.register_device_store(DeviceStore(hass))
    await trackers.async_load_devices()

    ws_url = BRIDGE_WS_URL

    token = entry.data.get("token", "")
    service_id = entry.data.get("service_id", "")

    report_location = _entry_value(entry, CONF_REPORT_LOCATION) is True
    reporter = None
    if token:
        client = HassBoxDataClient(hass, {"token": token})
        reporter = LocationReporter(hass, client, enabled=report_location)

    def on_location(device: str, fix) -> None:

        trackers.handle_location(device, fix)

        data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        reporter_ref = data.get("reporter") if data else None
        if reporter_ref is not None:
            reporter_ref.report(device, fix)

    def on_unlocated(device: str, fix) -> None:

        trackers.handle_unlocated(device, fix)

    bridge = BridgeManager(
        hass,
        ws_url,
        token,
        service_id,
        on_location,
        trackers.handle_state,
        trackers.handle_devices,
        on_unlocated,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_STORE: store,
        DATA_TRACKERS: trackers,
        DATA_BRIDGE: bridge,
        "reporter": reporter,
        "on_location": on_location,
        "on_unlocated": on_unlocated,
        "entry": entry,
    }

    async_register_websocket_commands(hass)

    integration = await async_get_integration(hass, DOMAIN)
    await async_register_frontend(hass, integration.version)

    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform.DEVICE_TRACKER, Platform.SENSOR]
    )

    devices: list = []
    if token:
        try:
            devices = await HassBoxDataClient(hass, {"token": token}).get_devices()
        except Exception:
            _LOGGER.warning(
                "[hassbox_map] 获取设备列表失败，跳过 bridge 连接", exc_info=True
            )
    if devices:

        trackers.handle_devices(devices)
        bridge.start()
    else:
        _LOGGER.debug(
            "[hassbox_map] 服务端未返回设备，暂不连接 bridge（绑定 GPS 设备后重载集成即可连接）"
        )
    _LOGGER.debug(
        "HassBox 地图 集成已加载（面板路由 /%s，bridge 鉴权后自动订阅绑定设备）",
        PANEL_URL_PATH,
    )
    return True

async def async_apply_account(
    hass: HomeAssistant, entry: ConfigEntry, token: str, service_id: str
) -> None:

    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is None:
        _LOGGER.warning("[hassbox_map] 集成数据不存在，跳过凭证热更新")
        return

    old_bridge = data.get(DATA_BRIDGE)
    if old_bridge is not None:
        await old_bridge.stop()
    data[DATA_BRIDGE] = None

    old_reporter = data.get("reporter")
    enabled = (
        old_reporter.enabled
        if old_reporter is not None
        else _entry_value(entry, CONF_REPORT_LOCATION) is True
    )
    data["reporter"] = None

    if not token:
        _LOGGER.debug("[hassbox_map] 账号已解绑，已停止定位上报与 bridge 连接")
        return

    client = HassBoxDataClient(hass, {"token": token})
    data["reporter"] = LocationReporter(hass, client, enabled=enabled)

    devices: list = []
    try:
        devices = await client.get_devices()
    except Exception:
        _LOGGER.warning("[hassbox_map] 绑定后获取设备列表失败", exc_info=True)

    if not devices:
        _LOGGER.debug(
            "[hassbox_map] 绑定成功但服务端暂无设备，暂不连接 bridge"
        )
        return

    trackers: TrackerManager = data[DATA_TRACKERS]
    trackers.handle_devices(devices)
    bridge = BridgeManager(
        hass,
        BRIDGE_WS_URL,
        token,
        service_id,
        data["on_location"],
        trackers.handle_state,
        trackers.handle_devices,
        data.get("on_unlocated"),
    )
    data[DATA_BRIDGE] = bridge
    bridge.start()
    _LOGGER.debug(
        "[hassbox_map] 账号绑定已即时生效（service=%s，设备 %d 台）",
        service_id,
        len(devices),
    )

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:

    from homeassistant.components.frontend import async_remove_panel

    async_remove_panel(hass, PANEL_URL_PATH)
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        await hass.config_entries.async_unload_platforms(
            entry, [Platform.DEVICE_TRACKER, Platform.SENSOR]
        )
        bridge = data.get(DATA_BRIDGE)
        if bridge is not None:
            await bridge.stop()
    return True
