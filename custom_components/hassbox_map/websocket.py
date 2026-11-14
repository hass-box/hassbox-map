from __future__ import annotations

import logging
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import (
    ActiveConnection,
    websocket_command,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_REPORT_LOCATION,
    CONF_TENCENT_MAP_KEY,
    DATA_BRIDGE,
    DATA_FENCE_MANAGER,
    DATA_FENCES_SIGNAL,
    DATA_STORE,
    DATA_TRACKERS,
    DATA_WS_REGISTERED,
    DOMAIN,
    WS_TYPE_CHECK_BIND,
    WS_TYPE_BIND_DEVICE,
    WS_TYPE_DELETE_GEOFENCE,
    WS_TYPE_GET_ACCOUNT,
    WS_TYPE_GET_CONFIG,
    WS_TYPE_GET_QR_CODE,
    WS_TYPE_GET_TRACK,
    WS_TYPE_LIST_GEOFENCES,
    WS_TYPE_REFRESH_LOCATION,
    WS_TYPE_REBOOT_DEVICE,
    WS_TYPE_SAVE_GEOFENCE,
    WS_TYPE_SET_MAP_KEY,
    WS_TYPE_SET_REPORT_INTERVAL,
    WS_TYPE_SET_REPORT_LOCATION,
    WS_TYPE_SET_TIMER_POWER,
    WS_TYPE_UNBIND_ACCOUNT,
    WS_TYPE_UNBIND_DEVICE,
)
from .storage import GeofenceStore
from .data_client import HassBoxDataClient
from .utils.store import async_load_from_store, async_save_to_store

_LOGGER = logging.getLogger(__name__)

STORE_CONFIG_KEY = "hassbox_map.config"

@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:

    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_WS_REGISTERED):
        return
    websocket_api.async_register_command(hass, ws_get_config)
    websocket_api.async_register_command(hass, ws_get_track)
    websocket_api.async_register_command(hass, ws_list_geofences)
    websocket_api.async_register_command(hass, ws_save_geofence)
    websocket_api.async_register_command(hass, ws_delete_geofence)
    websocket_api.async_register_command(hass, ws_set_report_interval)
    websocket_api.async_register_command(hass, ws_set_timer_power)
    websocket_api.async_register_command(hass, ws_refresh_location)
    websocket_api.async_register_command(hass, ws_reboot_device)
    websocket_api.async_register_command(hass, ws_get_account)
    websocket_api.async_register_command(hass, ws_get_qr_code)
    websocket_api.async_register_command(hass, ws_check_bind)
    websocket_api.async_register_command(hass, ws_unbind_account)
    websocket_api.async_register_command(hass, ws_bind_device)
    websocket_api.async_register_command(hass, ws_unbind_device)
    websocket_api.async_register_command(hass, ws_set_map_key)
    websocket_api.async_register_command(hass, ws_set_report_location)
    domain_data[DATA_WS_REGISTERED] = True

def _get_store(hass: HomeAssistant) -> GeofenceStore:

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and DATA_STORE in entry_data:
            return entry_data[DATA_STORE]
    raise RuntimeError("hassbox_map 集成未加载")

def _get_trackers(hass: HomeAssistant):

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and (trackers := entry_data.get(DATA_TRACKERS)):
            return trackers
    raise RuntimeError("hassbox_map 集成未加载")

def _get_bridge(hass: HomeAssistant):

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and (bridge := entry_data.get(DATA_BRIDGE)):
            return bridge
    raise RuntimeError("hassbox_map 集成未加载")

def _get_entry(hass: HomeAssistant):

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and (entry := entry_data.get("entry")):
            return entry
    raise RuntimeError("hassbox_map 集成未加载")

def _get_reporter(hass: HomeAssistant):

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and "reporter" in entry_data:
            return entry_data["reporter"]
    return None

def _get_fence_manager(hass: HomeAssistant):

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and (
            manager := entry_data.get(DATA_FENCE_MANAGER)
        ):
            return manager
    return None

def _resolve_device_sn(hass: HomeAssistant, trackers, entity_id: str) -> str:

    state = hass.states.get(entity_id)
    if state is not None:
        sn = state.attributes.get("device")
        if sn:
            return str(sn)
    for sn, entity in trackers.entities.items():
        if entity.entity_id == entity_id:
            return sn
    return ""

def _get_map_key(hass: HomeAssistant) -> str:

    return _get_entry_option(hass, CONF_TENCENT_MAP_KEY)

def _get_entry_option(hass: HomeAssistant, key: str) -> str:

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and (entry := entry_data.get("entry")):
            return str(entry.options.get(key, entry.data.get(key, "")) or "").strip()
    return ""

def _get_entry_flag(hass: HomeAssistant, key: str) -> bool:

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and (entry := entry_data.get("entry")):
            return bool(entry.options.get(key, entry.data.get(key, False)))
    return False

async def _async_collect_points(
    hass: HomeAssistant,
    entity_id: str,
    start_ts: int,
    end_ts: int,
    limit: int,
) -> list[dict[str, Any]]:

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import (
        state_changes_during_period,
    )

    start = dt_util.utc_from_timestamp(start_ts)
    end = dt_util.utc_from_timestamp(end_ts)

    states_map = await get_instance(hass).async_add_executor_job(
        partial(
            state_changes_during_period,
            hass,
            start,
            end_time=end,
            entity_id=entity_id,
            no_attributes=False,
        )
    )
    states = states_map.get(entity_id, [])

    points: list[dict[str, Any]] = []
    for state in states:
        attrs = state.attributes
        if "latitude" not in attrs or "longitude" not in attrs:
            continue
        point: dict[str, Any] = {
            "lat": float(attrs["latitude"]),
            "lng": float(attrs["longitude"]),
            "timestamp": int(state.last_updated.timestamp()),
        }

        for src, dst in (
            ("speed", "speed"),
            ("speed_kmh", "speed_kmh"),
            ("altitude", "altitude"),
            ("direction", "direction"),
            ("gps_accuracy", "accuracy"),
        ):
            if src in attrs:
                try:
                    point[dst] = float(attrs[src])
                except (TypeError, ValueError):
                    pass
        points.append(point)

    return points[-limit:]

@websocket_command({vol.Required("type"): WS_TYPE_GET_CONFIG})
@websocket_api.async_response
async def ws_get_config(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    connection.send_result(msg["id"], {"tencent_map_key": _get_map_key(hass)})

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_TRACK,
        vol.Required("device_id"): str,
        vol.Required("start"): vol.Coerce(int),
        vol.Required("end"): vol.Coerce(int),
        vol.Optional("limit", default=5000): vol.Coerce(int),
    }
)
@websocket_api.async_response
async def ws_get_track(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        points = await _async_collect_points(
            hass, msg["device_id"], msg["start"], msg["end"], msg["limit"]
        )
        connection.send_result(msg["id"], {"points": points})
    except Exception as err:
        connection.send_error(msg["id"], "track_failed", str(err))

@websocket_command({vol.Required("type"): WS_TYPE_LIST_GEOFENCES})
@websocket_api.async_response
async def ws_list_geofences(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        manager = _get_fence_manager(hass)
        geofences: list[dict[str, Any]] = []
        for fence in _get_store(hass).list():
            fid = fence.get("id") or ""
            name = manager.fence_name(fid) if manager is not None else ""
            geofences.append({**fence, "name": name or "围栏"})
        connection.send_result(msg["id"], {"geofences": geofences})
    except Exception as err:
        connection.send_error(msg["id"], "list_failed", str(err))

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_SAVE_GEOFENCE,
        vol.Optional("fence_id"): str,
        vol.Required("name"): vol.All(str, vol.Length(min=1)),

        vol.Required("polygons"): vol.All(
            [
                vol.All(
                    [{"lat": vol.Coerce(float), "lng": vol.Coerce(float)}],
                    vol.Length(min=3),
                )
            ],
            vol.Length(min=1),
        ),
        vol.Optional("enabled", default=True): bool,
    }
)
@websocket_api.async_response
async def ws_save_geofence(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        data = {
            k: msg[k]
            for k in ("name", "polygons", "enabled")
            if k in msg
        }

        if "fence_id" in msg and msg["fence_id"]:
            data["id"] = msg["fence_id"]
        fence = await _get_store(hass).async_save(data)

        manager = _get_fence_manager(hass)
        if manager is not None:
            manager.async_set_fence_name(fence["id"], msg["name"])

        async_dispatcher_send(hass, DATA_FENCES_SIGNAL)
        connection.send_result(
            msg["id"], {"success": True, **fence, "name": msg["name"]}
        )
    except Exception as err:
        connection.send_error(msg["id"], "save_failed", str(err))

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_GEOFENCE,
        vol.Required("fence_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_geofence(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        ok = await _get_store(hass).async_delete(msg["fence_id"])

        async_dispatcher_send(hass, DATA_FENCES_SIGNAL)
        connection.send_result(msg["id"], {"success": ok})
    except Exception as err:
        connection.send_error(msg["id"], "delete_failed", str(err))

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_REPORT_INTERVAL,
        vol.Required("device_id"): str,
        vol.Required("interval"): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
        vol.Optional("sleep_interval"): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=86400)
        ),
    }
)
@websocket_api.async_response
async def ws_set_report_interval(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    device_id = msg["device_id"]
    interval = msg["interval"]
    sleep_interval = msg.get("sleep_interval")

    trackers = _get_trackers(hass)
    sn = _resolve_device_sn(hass, trackers, device_id)
    if not sn:
        connection.send_error(
            msg["id"], "device_not_found", f"未找到设备 {device_id}"
        )
        return

    try:
        await _get_bridge(hass).async_set_report_interval(
            sn, interval, sleep_interval
        )
    except Exception as err:

        _LOGGER.warning(
            "[hassbox_map] 设置上报间隔失败 device=%s sn=%s interval=%d",
            device_id, sn, interval, exc_info=True,
        )
        connection.send_error(msg["id"], "interval_failed", str(err))
        return

    trackers.set_report_interval(sn, interval, sleep_interval)
    connection.send_result(
        msg["id"],
        {
            "success": True,
            "device": sn,
            "interval": interval,
            "sleep_interval": sleep_interval,
        },
    )

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_TIMER_POWER,
        vol.Required("device_id"): str,
        vol.Required("timer_power"): vol.Schema(
            {
                vol.Required("enabled"): bool,
                vol.Optional("mode", default=0): vol.All(
                    vol.Coerce(int), vol.In([0, 1, 2])
                ),
                vol.Optional("ranges", default=list): [
                    vol.Schema(
                        {
                            vol.Required("start"): str,
                            vol.Required("end"): str,
                        }
                    )
                ],
                vol.Optional("dates", default=list): [
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=31))
                ],
            }
        ),
    }
)
@websocket_api.async_response
async def ws_set_timer_power(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    device_id = msg["device_id"]
    config = msg["timer_power"]

    trackers = _get_trackers(hass)
    sn = _resolve_device_sn(hass, trackers, device_id)
    if not sn:
        connection.send_error(
            msg["id"], "device_not_found", f"未找到设备 {device_id}"
        )
        return

    try:
        await _get_bridge(hass).async_set_timer_power(sn, config)
    except Exception as err:
        _LOGGER.warning(
            "[hassbox_map] 设置定时开关机失败 device=%s sn=%s",
            device_id, sn, exc_info=True,
        )
        connection.send_error(msg["id"], "timer_power_failed", str(err))
        return

    trackers.set_timer_power(sn, config)
    connection.send_result(
        msg["id"],
        {
            "success": True,
            "device": sn,
            "timer_power": config,
        },
    )

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_REFRESH_LOCATION,
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def ws_refresh_location(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    device_id = msg["device_id"]

    trackers = _get_trackers(hass)
    sn = _resolve_device_sn(hass, trackers, device_id)
    if not sn:
        connection.send_error(
            msg["id"], "device_not_found", f"未找到设备 {device_id}"
        )
        return

    try:
        await _get_bridge(hass).async_refresh_location(sn)
    except Exception as err:

        _LOGGER.warning(
            "[hassbox_map] 刷新当前定位失败 device=%s sn=%s",
            device_id, sn, exc_info=True,
        )
        connection.send_error(msg["id"], "refresh_location_failed", str(err))
        return

    connection.send_result(msg["id"], {"success": True, "device": sn})

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_REBOOT_DEVICE,
        vol.Required("device_id"): str,
    }
)
@websocket_api.async_response
async def ws_reboot_device(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    device_id = msg["device_id"]

    trackers = _get_trackers(hass)
    sn = _resolve_device_sn(hass, trackers, device_id)
    if not sn:
        connection.send_error(
            msg["id"], "device_not_found", f"未找到设备 {device_id}"
        )
        return

    try:
        await _get_bridge(hass).async_reboot_device(sn)
    except Exception as err:

        _LOGGER.warning(
            "[hassbox_map] 重启设备失败 device=%s sn=%s",
            device_id, sn, exc_info=True,
        )
        connection.send_error(msg["id"], "reboot_failed", str(err))
        return

    connection.send_result(msg["id"], {"success": True, "device": sn})

async def _async_account(hass: HomeAssistant) -> tuple[str, str]:

    entry = _get_entry(hass)
    token = str(entry.data.get("token") or "")
    service_id = str(entry.data.get("service_id") or "")
    if not token:
        config = await async_load_from_store(hass, STORE_CONFIG_KEY) or {}
        token = str(config.get("token") or "")
        service_id = service_id or str(config.get("service_id") or "")
    return token, service_id

@websocket_command({vol.Required("type"): WS_TYPE_GET_ACCOUNT})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_account(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        token, service_id = await _async_account(hass)
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 获取账号信息失败", exc_info=True)
        connection.send_error(msg["id"], "account_failed", str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "bound": bool(token),
            "service_id": service_id,
            "report_location": _get_entry_flag(hass, CONF_REPORT_LOCATION),
        },
    )

@websocket_command({vol.Required("type"): WS_TYPE_GET_QR_CODE})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get_qr_code(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    client = HassBoxDataClient(hass)
    try:
        result = await client.get_qrcode()
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 获取绑定二维码失败", exc_info=True)
        connection.send_error(msg["id"], "qrcode_failed", str(err))
        return
    connection.send_result(msg["id"], result)

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_CHECK_BIND,
        vol.Required("token"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_check_bind(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    client = HassBoxDataClient(hass, {"token": msg["token"]})
    try:
        result = await client.check_state()
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 检查绑定状态失败", exc_info=True)
        connection.send_error(msg["id"], "bind_failed", str(err))
        return

    if "token" not in result:
        connection.send_result(
            msg["id"],
            {"bound": False, "errmsg": result.get("errmsg", "绑定失败")},
        )
        return

    service_id = result.get("serviceId") or result.get("service_id") or ""
    try:
        entry = _get_entry(hass)
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "token": result["token"], "service_id": service_id},
        )
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 保存绑定信息失败", exc_info=True)
        connection.send_error(msg["id"], "bind_save_failed", str(err))
        return

    connection.send_result(
        msg["id"], {"bound": True, "service_id": service_id}
    )

    from . import async_apply_account

    hass.async_create_task(
        async_apply_account(hass, entry, result["token"], service_id)
    )

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_UNBIND_ACCOUNT,
        vol.Optional("service_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_unbind_account(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        entry = _get_entry(hass)
    except Exception as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return

    service_id = msg.get("service_id") or entry.data.get("service_id") or ""
    if not service_id:
        connection.send_error(msg["id"], "not_bound", "当前未绑定账号")
        return

    client = HassBoxDataClient(hass, {"token": entry.data.get("token", "")})
    try:
        result = await client.unbind(service_id)
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 解绑账号失败", exc_info=True)
        connection.send_error(msg["id"], "unbind_failed", str(err))
        return

    if result.get("errcode") != 0:
        connection.send_result(
            msg["id"],
            {"success": False, "errmsg": result.get("errmsg", "解绑失败")},
        )
        return

    try:
        await async_save_to_store(
            hass, STORE_CONFIG_KEY, {"token": "", "service_id": ""}
        )
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": "", "service_id": ""}
        )
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 清理解绑信息失败", exc_info=True)
        connection.send_error(msg["id"], "unbind_save_failed", str(err))
        return

    connection.send_result(msg["id"], {"success": True})

    from . import async_apply_account

    hass.async_create_task(async_apply_account(hass, entry, "", ""))

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_BIND_DEVICE,
        vol.Required("sn"): str,
        vol.Optional("name"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bind_device(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        entry = _get_entry(hass)
    except Exception as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return

    service_id = str(entry.data.get("service_id") or "")
    token = str(entry.data.get("token") or "")
    if not service_id or not token:
        connection.send_error(msg["id"], "not_bound", "请先绑定 HassBox 地图账号")
        return

    sn = msg["sn"].strip()
    if not sn:
        connection.send_error(msg["id"], "invalid_sn", "设备 SN 不能为空")
        return
    name = (msg.get("name") or "").strip() or sn

    client = HassBoxDataClient(hass, {"token": token})
    try:
        result = await client.bind_device(service_id, sn, name)
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 绑定设备失败 sn=%s", sn, exc_info=True)
        connection.send_error(msg["id"], "bind_device_failed", str(err))
        return

    if result.get("errcode") not in (0, None):
        connection.send_result(
            msg["id"],
            {"success": False, "errmsg": result.get("errmsg", "绑定失败")},
        )
        return

    refreshed = await _refresh_device_list(hass)
    connection.send_result(
        msg["id"], {"success": True, "sn": sn, "refreshed": refreshed}
    )

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_UNBIND_DEVICE,
        vol.Required("sn"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_unbind_device(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        entry = _get_entry(hass)
    except Exception as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return

    service_id = str(entry.data.get("service_id") or "")
    token = str(entry.data.get("token") or "")
    if not service_id or not token:
        connection.send_error(msg["id"], "not_bound", "请先绑定 HassBox 地图账号")
        return

    sn = msg["sn"].strip()
    if not sn:
        connection.send_error(msg["id"], "invalid_sn", "设备 SN 不能为空")
        return

    client = HassBoxDataClient(hass, {"token": token})
    try:
        result = await client.unbind_device(service_id, sn)
    except Exception as err:
        _LOGGER.warning("[hassbox_map] 解绑设备失败 sn=%s", sn, exc_info=True)
        connection.send_error(msg["id"], "unbind_device_failed", str(err))
        return

    if result.get("errcode") not in (0, None):
        connection.send_result(
            msg["id"],
            {"success": False, "errmsg": result.get("errmsg", "解绑失败")},
        )
        return

    refreshed = await _refresh_device_list(hass)
    connection.send_result(
        msg["id"], {"success": True, "sn": sn, "refreshed": refreshed}
    )

async def _refresh_device_list(hass: HomeAssistant) -> bool:

    bridge = _get_bridge(hass)
    try:
        if bridge.started:
            return await bridge.async_refresh_devices()
        devices = await HassBoxDataClient(hass, {"token": bridge.token}).get_devices()
        if devices:
            bridge.start()
        return bool(devices)
    except Exception:
        _LOGGER.warning("[hassbox_map] 刷新设备列表失败", exc_info=True)
        return False

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_MAP_KEY,
        vol.Required("tencent_map_key"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_map_key(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        entry = _get_entry(hass)
    except Exception as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return

    key = msg["tencent_map_key"].strip()
    if not key:
        connection.send_error(msg["id"], "invalid_key", "腾讯地图 Key 不能为空")
        return

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_TENCENT_MAP_KEY: key}
    )
    connection.send_result(
        msg["id"], {"success": True, "tencent_map_key": key}
    )

@websocket_command(
    {
        vol.Required("type"): WS_TYPE_SET_REPORT_LOCATION,
        vol.Required("report_location"): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_report_location(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:

    try:
        entry = _get_entry(hass)
    except Exception as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return

    enabled = bool(msg["report_location"])
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_REPORT_LOCATION: enabled}
    )

    reporter = _get_reporter(hass)
    if reporter is not None:
        reporter.enabled = enabled
    connection.send_result(
        msg["id"], {"success": True, "report_location": enabled}
    )
