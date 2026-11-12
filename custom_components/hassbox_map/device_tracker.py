from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any

from homeassistant.components import zone
from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DATA_TRACKERS, DOMAIN
from .jt808 import LocationFix
from .storage import DeviceStore

_LOGGER = logging.getLogger(__name__)

MAX_SPEED_MPS = 55.0
STATIONARY_DRIFT_M = 50.0
MIN_MOVE_M = 30.0
STATIONARY_SPEED_KMH = 1.5

PIN_SPEED_KMH = 3.0
PIN_RADIUS_M = STATIONARY_DRIFT_M

CONFIRM_FOLLOW_UPS = 2

_EARTH_RADIUS_M = 6371008.8

ALT_MAX_RATE_MPS = 8.0
ALT_SMOOTH_ALPHA = 0.5
ALT_DT_MIN = 1.0
ALT_DT_MAX = 60.0

class AltitudeFilter:

    def __init__(self) -> None:
        self.value: int | None = None
        self._last_time: datetime | None = None

    def update(self, fix: LocationFix) -> int | None:
        raw = fix.altitude
        if raw is None:
            fix.altitude = self.value
            return self.value

        if self.value is None or self._last_time is None:
            self.value = raw
            self._last_time = fix.device_time
            fix.altitude = raw
            return raw

        if not fix.moving and fix.speed_kmh < STATIONARY_SPEED_KMH:

            self._last_time = fix.device_time
            fix.altitude = self.value
            return self.value

        dt = (fix.device_time - self._last_time).total_seconds()
        dt = min(max(dt, ALT_DT_MIN), ALT_DT_MAX)
        max_delta = ALT_MAX_RATE_MPS * dt
        delta = max(-max_delta, min(max_delta, raw - self.value))
        self.value += round(delta * ALT_SMOOTH_ALPHA)
        self._last_time = fix.device_time
        fix.altitude = self.value
        return self.value

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:

    d_lat = radians(lat2 - lat1)
    d_lng = radians(lng2 - lng1)
    h = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lng / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * asin(min(1.0, sqrt(h)))

@dataclass
class PositionDeviation:

    reason: str
    distance_m: float
    raw_latitude: float
    raw_longitude: float

def _fix_from_state(state: State) -> LocationFix | None:

    attributes = state.attributes
    latitude = attributes.get("latitude")
    longitude = attributes.get("longitude")
    if latitude is None or longitude is None:
        return None
    try:
        device_time = datetime.fromisoformat(str(attributes["device_time"]))
    except (KeyError, TypeError, ValueError):
        device_time = state.last_updated
    try:
        raw_altitude = attributes.get("altitude")
        return LocationFix(
            latitude=float(latitude),
            longitude=float(longitude),
            altitude=int(raw_altitude) if raw_altitude is not None else None,
            speed_kmh=float(attributes.get("speed_kmh") or 0),
            direction=int(attributes.get("direction") or 0),
            alarm=int(attributes.get("alarm") or 0),
            status=int(attributes.get("status") or 0),
            device_time=device_time,
            msg_id=0,
            gps_fix=bool(attributes.get("gps_fix")),
            moving=bool(attributes.get("moving")),
            extras=dict(attributes.get("extras") or {}),
        )
    except (TypeError, ValueError):
        return None

class TrackerManager:

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entities: dict[str, HassBoxMapTracker] = {}
        self.sensors: dict[str, list] = {}
        self.fixes: dict[str, LocationFix] = {}

        self.last_report: dict[str, datetime] = {}

        self._last_extras: dict[str, dict[str, Any]] = {}
        self.names: dict[str, str] = {}

        self.infos: dict[str, dict[str, Any]] = {}
        self._online: set[str] = set()
        self.report_intervals: dict[str, int] = {}
        self.sleep_report_intervals: dict[str, int] = {}

        self.timer_powers: dict[str, dict[str, Any]] = {}

        self.timer_power_support: dict[str, bool] = {}
        self._known: set[str] = set()
        self._device_store: DeviceStore | None = None
        self._add_entities = None
        self._add_sensors = None

        self._pending_fix: dict[str, LocationFix] = {}
        self._pending_hits: dict[str, int] = {}

        self._pinned: set[str] = set()

    @callback
    def register_device_store(self, store: DeviceStore) -> None:

        self._device_store = store

    async def async_load_devices(self) -> None:

        if self._device_store is None:
            return
        await self._device_store.async_load()
        for item in self._device_store.list():
            sn = item.get("sn")
            if not sn:
                continue
            self._known.add(sn)
            name = item.get("name")
            if name:
                self.names[sn] = str(name)
        if self._known:
            _LOGGER.debug("恢复历史设备 %d 台", len(self._known))

    @callback
    def set_report_interval(
        self, device: str, interval: int, sleep_interval: int | None = None
    ) -> None:

        self.report_intervals[device] = interval
        if sleep_interval:
            self.sleep_report_intervals[device] = sleep_interval
        entity = self.entities.get(device)
        if entity is not None:
            entity.async_write_ha_state()

    @callback
    def set_timer_power(self, device: str, config: dict[str, Any]) -> None:

        self.timer_powers[device] = dict(config)
        entity = self.entities.get(device)
        if entity is not None:
            entity.async_write_ha_state()

    @callback
    def known_devices(self) -> list[str]:

        return sorted(self._known | set(self.fixes) | set(self.entities))

    @callback
    def device_details(self, device: str) -> dict[str, str | None]:

        info = self.infos.get(device) or {}

        def _text(key: str) -> str | None:
            value = info.get(key)
            return str(value) if value not in (None, "") else None

        return {
            "model": "HassBox GPS",
            "model_id": _text("model"),
            "serial_number": _text("iccid"),
            "hw_version": _text("deviceID"),
            "sw_version": _text("version"),
        }

    @callback
    def sync_device_registry(self, device: str) -> None:

        name = self.names.get(device) or device
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, device)},
            name=name,
            manufacturer="HassBox",
            **self.device_details(device),
        )

    @callback
    def register_add_entities(self, add_entities) -> None:

        self._add_entities = add_entities

    @callback
    def register_add_sensors(self, add_sensors) -> None:

        self._add_sensors = add_sensors

    @callback
    def handle_devices(self, devices: list[dict[str, Any]]) -> None:

        returned: set[str] = set()
        for item in devices or []:
            sn = (item or {}).get("sn")
            if not sn:
                continue
            name = item.get("name")
            if name:
                self.names[sn] = str(name)
            self._known.add(sn)
            returned.add(sn)

            try:
                interval = int(item.get("report_interval"))
            except (TypeError, ValueError):
                interval = 0
            interval_changed = interval > 0 and self.report_intervals.get(sn) != interval
            if interval_changed:
                self.report_intervals[sn] = interval

            try:
                sleep_interval = int(item.get("sleep_report_interval"))
            except (TypeError, ValueError):
                sleep_interval = 0
            if sleep_interval > 0 and self.sleep_report_intervals.get(sn) != sleep_interval:
                self.sleep_report_intervals[sn] = sleep_interval

            timer_power = item.get("timer_power")
            if isinstance(timer_power, dict) and self.timer_powers.get(sn) != timer_power:
                self.timer_powers[sn] = dict(timer_power)

            supported = bool(item.get("timer_power_supported"))
            if self.timer_power_support.get(sn) != supported:
                self.timer_power_support[sn] = supported

            _LOGGER.debug("[bridge] 设备信息全部 sn=%s item=%s", sn, item)
            info_changed = False
            info = item.get("info")
            if isinstance(info, dict) and info and self.infos.get(sn) != info:
                self.infos[sn] = dict(info)
                info_changed = True
            existing = self.entities.get(sn)
            self._ensure_entities(sn)
            if info_changed:

                self.sync_device_registry(sn)

            if existing is not None:
                existing.async_write_ha_state()

        removed = sorted(set(self._known) - returned)
        if removed:
            self.hass.async_create_task(self._async_remove_devices(removed))

        if self._device_store is not None:
            self.hass.async_create_task(self._device_store.async_remember(devices))

    async def _async_remove_devices(self, devices: list[str]) -> None:

        entity_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)

        for device in devices:
            for sensor in self.sensors.pop(device, []):
                if sensor.entity_id:
                    entity_reg.async_remove(sensor.entity_id)
            entity = self.entities.pop(device, None)
            if entity is not None and entity.entity_id:
                entity_reg.async_remove(entity.entity_id)

            self.fixes.pop(device, None)
            self.names.pop(device, None)
            self.report_intervals.pop(device, None)
            self.sleep_report_intervals.pop(device, None)
            self.timer_powers.pop(device, None)
            self.timer_power_support.pop(device, None)
            self.last_report.pop(device, None)
            self._last_extras.pop(device, None)
            self._known.discard(device)
            self._online.discard(device)
            self._pinned.discard(device)
            self._pending_fix.pop(device, None)
            self._pending_hits.pop(device, None)

            device_entry = device_reg.async_get_device(identifiers={(DOMAIN, device)})
            if device_entry is not None:
                device_reg.async_remove_device(device_entry.id)

        if self._device_store is not None:
            await self._device_store.async_forget(devices)
        _LOGGER.debug("已移除服务端解绑设备 device=%s", ", ".join(devices))

    def _ensure_entities(self, device: str):

        entity = self.entities.get(device)
        if entity is None and self._add_entities is not None:
            entity = HassBoxMapTracker(self, device)
            self.entities[device] = entity
            self._add_entities([entity])
            _LOGGER.debug("device_tracker 新建实体 device=%s", device)

        sensors = self.sensors.get(device)
        if sensors is None and self._add_sensors is not None:
            from .sensor import SENSOR_DESCRIPTIONS, HassBoxMapSensor

            sensors = [
                HassBoxMapSensor(self, device, desc) for desc in SENSOR_DESCRIPTIONS
            ]
            self.sensors[device] = sensors
            self._add_sensors(sensors)
            _LOGGER.debug("sensor 新建实体 device=%s 共 %d 个", device, len(sensors))
        if sensors is not None:
            self._ensure_battery_sensor(device, sensors)
        return entity, sensors

    @callback
    def _ensure_battery_sensor(self, device: str, sensors: list) -> None:

        if self._add_sensors is None:
            return
        fix = self.fixes.get(device)
        extras = fix.extras if fix is not None else self._last_extras.get(device, {})
        if extras.get("power_source") != "battery":
            return
        if any(s.entity_description.key == "battery" for s in sensors):
            return

        from .sensor import BATTERY_DESCRIPTION, HassBoxMapSensor

        sensor = HassBoxMapSensor(self, device, BATTERY_DESCRIPTION)
        sensors.append(sensor)
        self._add_sensors([sensor])
        _LOGGER.debug("sensor 新建电量实体 device=%s", device)

    @callback
    def handle_location(self, device: str, fix: LocationFix) -> None:

        pinned = False
        deviation: PositionDeviation | None = None
        try:
            effective, pinned = self._pin_stationary(device, fix)
            if not pinned:
                effective, deviation = self._guard_deviation(device, effective)
        except Exception:

            _LOGGER.error(
                "定位偏移判定异常 device=%s，按原始帧处理", device, exc_info=True
            )
            effective, deviation, pinned = fix, None, False

        if deviation is not None:
            _LOGGER.warning(
                "[device_tracker] 定位偏移已回退 device=%s reason=%s "
                "原始=(%.6f,%.6f) 回退=(%.6f,%.6f) 偏离=%.0fm",
                device, deviation.reason,
                deviation.raw_latitude, deviation.raw_longitude,
                effective.latitude, effective.longitude, deviation.distance_m,
            )

        prev = self.fixes.get(device)
        if effective.extras and (prev is None or prev.extras != effective.extras):
            _LOGGER.warning(
                "[jt808] extras device=%s time=%s extras=%s",
                device,
                effective.device_time.isoformat(),
                effective.extras,
            )

        self.fixes[device] = effective
        entity, sensors = self._ensure_entities(device)
        if entity is not None:
            entity.set_fix(effective, deviation, pinned)
            entity.async_write_ha_state()
        for sensor in sensors or []:
            sensor.set_fix(effective)

            if sensor.entity_id:
                sensor.async_write_ha_state()

        if entity is None and self._add_entities is None:
            _LOGGER.debug("device_tracker 平台未就绪，缓冲 device=%s", device)

    @callback
    def handle_unlocated(self, device: str, fix: LocationFix) -> None:

        self.last_report[device] = fix.device_time
        if fix.extras:
            self._last_extras[device] = dict(fix.extras)
        entity, _sensors = self._ensure_entities(device)
        if entity is None:

            return
        entity.update_report_time(fix.device_time, fix.extras)
        entity.async_write_ha_state()

        for sensor in _sensors or []:
            if sensor.entity_description.key != "battery":
                continue
            sensor.set_fix(fix)
            if sensor.entity_id:
                sensor.async_write_ha_state()

    @callback
    def _pin_stationary(
        self, device: str, fix: LocationFix
    ) -> tuple[LocationFix, bool]:

        anchor = self.fixes.get(device)
        if anchor is None or not fix.gps_fix:
            return fix, False

        distance = _haversine_m(
            anchor.latitude, anchor.longitude, fix.latitude, fix.longitude
        )
        stationary = not fix.moving and fix.speed_kmh < PIN_SPEED_KMH
        if not stationary or distance > PIN_RADIUS_M:
            self._unpin(device, distance, fix.speed_kmh)
            return fix, False

        if device not in self._pinned:
            self._pinned.add(device)

            self._clear_pending(device)
            _LOGGER.warning(
                "[device_tracker] 位置已钉住 device=%s anchor=(%.6f,%.6f) "
                "原始偏移=%.0fm speed=%.1fkm/h",
                device, anchor.latitude, anchor.longitude, distance, fix.speed_kmh,
            )

        return (
            replace(fix, latitude=anchor.latitude, longitude=anchor.longitude),
            True,
        )

    def _unpin(self, device: str, distance: float, speed_kmh: float) -> None:

        if device not in self._pinned:
            return
        self._pinned.discard(device)
        _LOGGER.debug(
            "[device_tracker] 解除位置钉住 device=%s 距锚点=%.0fm speed=%.1fkm/h",
            device, distance, speed_kmh,
        )

    @callback
    def _guard_deviation(
        self, device: str, fix: LocationFix
    ) -> tuple[LocationFix, PositionDeviation | None]:

        anchor = self.fixes.get(device)
        if anchor is None:
            self._clear_pending(device)
            return fix, None

        distance = _haversine_m(
            anchor.latitude, anchor.longitude, fix.latitude, fix.longitude
        )
        elapsed = (fix.device_time - anchor.device_time).total_seconds()

        if elapsed <= 0:
            if distance <= MIN_MOVE_M:
                self._clear_pending(device)
                return fix, None
            reason = (
                f"设备时间未推进（dt={elapsed:.0f}s）且位移 {distance:.0f}m 无法校验"
            )
        elif distance > MAX_SPEED_MPS * elapsed:
            reason = (
                f"隐含速度 {distance / elapsed * 3.6:.0f}km/h"
                f" 超过上限 {MAX_SPEED_MPS * 3.6:.0f}km/h"
            )
        elif (
            not fix.moving
            and fix.speed_kmh < STATIONARY_SPEED_KMH
            and distance > STATIONARY_DRIFT_M
        ):
            reason = f"设备自报静止但坐标偏移 {distance:.0f}m"
        else:
            self._clear_pending(device)
            return fix, None

        pending = self._pending_fix.get(device)
        if pending is not None and self._same_place(pending, fix):
            hits = self._pending_hits.get(device, 0) + 1
            if hits >= CONFIRM_FOLLOW_UPS:
                self._clear_pending(device)
                _LOGGER.warning(
                    "[device_tracker] 偏移经 %d 帧连续确认，接受新位置 device=%s "
                    "lat=%.6f lng=%.6f（原判定：%s）",
                    hits + 1, device, fix.latitude, fix.longitude, reason,
                )
                return fix, None
            self._pending_hits[device] = hits
        else:
            self._pending_fix[device] = fix
            self._pending_hits[device] = 0

        return (
            replace(fix, latitude=anchor.latitude, longitude=anchor.longitude),
            PositionDeviation(
                reason=reason,
                distance_m=distance,
                raw_latitude=fix.latitude,
                raw_longitude=fix.longitude,
            ),
        )

    def _same_place(self, pending: LocationFix, fix: LocationFix) -> bool:

        elapsed = (fix.device_time - pending.device_time).total_seconds()
        if elapsed <= 0:
            return False
        if elapsed > 600:
            return False
        distance = _haversine_m(
            pending.latitude, pending.longitude, fix.latitude, fix.longitude
        )
        return distance <= max(MIN_MOVE_M, MAX_SPEED_MPS * elapsed)

    def _clear_pending(self, device: str) -> None:
        self._pending_fix.pop(device, None)
        self._pending_hits.pop(device, None)

    @callback
    def handle_state(self, device: str, online: bool) -> None:

        if online:
            self._online.add(device)
        else:
            self._online.discard(device)
        entity = self.entities.get(device)
        if entity is not None:
            entity.set_online(online)
            entity.async_write_ha_state()
        for sensor in self.sensors.get(device, []):
            sensor.set_online(online)
            sensor.async_write_ha_state()

    @callback
    def is_online(self, device: str) -> bool:
        return device in self._online

class HassBoxMapTracker(RestoreEntity, TrackerEntity):

    _attr_should_poll = False
    _attr_source_type = SourceType.GPS

    def __init__(self, manager: TrackerManager, device: str) -> None:
        self._manager = manager
        self._device = device
        self._fix = manager.fixes.get(device)

        self._report_time: datetime | None = manager.last_report.get(device)

        self._report_extras: dict[str, Any] = dict(
            manager._last_extras.get(device) or {}
        )
        self._online = manager.is_online(device)

        self._altitude_filter = AltitudeFilter()

        self._deviation: PositionDeviation | None = None

        self._pinned = False

        self._in_fence_zones: list[str] = []
        display = manager.names.get(device) or device
        self._attr_unique_id = f"{DOMAIN}_{device}"
        self._attr_name = display

    @property
    def device_info(self) -> DeviceInfo:

        display = self._manager.names.get(self._device) or self._device
        return DeviceInfo(
            identifiers={(DOMAIN, self._device)},
            name=display,
            manufacturer="HassBox",
            **self._manager.device_details(self._device),
        )

    async def async_added_to_hass(self) -> None:

        await super().async_added_to_hass()
        if self._fix is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        fix = _fix_from_state(last_state)
        if fix is None:
            return
        self.set_fix(fix)
        self.async_write_ha_state()

    @callback
    def set_fix(
        self,
        fix: LocationFix,
        deviation: PositionDeviation | None = None,
        pinned: bool = False,
    ) -> None:

        self._altitude_filter.update(fix)
        self._fix = fix

        self._report_time = fix.device_time
        self._deviation = deviation
        self._pinned = pinned
        self._attr_latitude = fix.latitude
        self._attr_longitude = fix.longitude
        self._attr_location_accuracy = 0 if fix.gps_fix else 50
        self._online = True

    @callback
    def update_report_time(
        self, device_time: datetime, extras: dict[str, Any] | None = None
    ) -> None:

        self._report_time = device_time
        if extras:
            self._report_extras = dict(extras)
        self._online = True

    @callback
    def set_online(self, online: bool) -> None:
        self._online = online

    @callback
    def set_in_zones(self, entity_ids: list[str]) -> None:

        if entity_ids == self._in_fence_zones:
            return
        self._in_fence_zones = entity_ids
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def in_zones(self) -> list[str] | None:

        if not self._in_fence_zones:
            return None
        zones = list(self._in_fence_zones)
        if (
            self.hass is not None
            and self.latitude is not None
            and self.longitude is not None
        ):
            _, ha_zones = zone.async_in_zones(
                self.hass, self.latitude, self.longitude, self.location_accuracy
            )
            zones.extend(eid for eid in ha_zones if eid not in zones)
        return zones

    @property
    def available(self) -> bool:

        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:

        attrs: dict[str, Any] = {
            "device": self._device,
            "online": self._online,
            "report_interval": self._manager.report_intervals.get(self._device, 5),
            "sleep_report_interval": self._manager.sleep_report_intervals.get(
                self._device, 1800
            ),
            "timer_power": self._manager.timer_powers.get(self._device, {}),
            "timer_power_supported": self._manager.timer_power_support.get(
                self._device, False
            ),
        }
        if self._fix is None:
            if self._report_time is not None:
                attrs["device_time"] = self._report_time.isoformat()
            if self._report_extras:
                attrs["extras"] = self._report_extras
            return attrs
        attrs.update(
            {
                "speed_kmh": self._fix.speed_kmh,
                "altitude": self._fix.altitude,
                "direction": self._fix.direction,
                "alarm": self._fix.alarm,
                "status": self._fix.status,
                "gps_fix": self._fix.gps_fix,
                "moving": self._fix.moving,
                "extras": self._fix.extras,
                "device_time": (self._report_time or self._fix.device_time).isoformat(),
                "position_corrected": self._deviation is not None,
                "position_pinned": self._pinned,
            }
        )

        if self._deviation is not None:
            attrs.update(
                {
                    "raw_latitude": self._deviation.raw_latitude,
                    "raw_longitude": self._deviation.raw_longitude,
                    "deviation_reason": self._deviation.reason,
                    "deviation_distance_m": round(self._deviation.distance_m, 1),
                }
            )
        return attrs

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:

    manager: TrackerManager = hass.data[DOMAIN][entry.entry_id][DATA_TRACKERS]
    manager.register_add_entities(async_add_entities)

    devices = set(manager.known_devices())

    new_entities: list[HassBoxMapTracker] = []
    for device in sorted(devices):
        if device not in manager.entities:
            entity = HassBoxMapTracker(manager, device)
            manager.entities[device] = entity
            new_entities.append(entity)
    if new_entities:
        async_add_entities(new_entities)
        _LOGGER.debug("device_tracker 平台就绪，实体 %d 个", len(new_entities))
