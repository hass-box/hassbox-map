from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import DATA_STORE, DATA_TRACKERS, DOMAIN
from .device_tracker import TrackerManager
from .fence_entities import async_setup_fence_entities
from .jt808 import LocationFix

_LOGGER = logging.getLogger(__name__)

SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="latitude",
        name="纬度",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:latitude",
    ),
    SensorEntityDescription(
        key="longitude",
        name="经度",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:longitude",
    ),
    SensorEntityDescription(
        key="speed",
        name="速度",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    SensorEntityDescription(
        key="altitude",
        name="海拔",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:elevation-rise",
    ),
    SensorEntityDescription(
        key="direction",
        name="方向",
        native_unit_of_measurement="°",
        icon="mdi:compass",
    ),
    SensorEntityDescription(
        key="device_time",
        name="定位时间",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
    ),
    SensorEntityDescription(
        key="gps_fix",
        name="定位状态",
        device_class=SensorDeviceClass.ENUM,
        options=["定位", "未定位"],
        icon="mdi:map-marker-question",
    ),
    SensorEntityDescription(
        key="moving",
        name="运动状态",
        device_class=SensorDeviceClass.ENUM,
        options=["移动", "静止"],
        icon="mdi:motion-sensor",
    ),
)

BATTERY_DESCRIPTION = SensorEntityDescription(
    key="battery",
    name="电池电量",
    device_class=SensorDeviceClass.BATTERY,
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:battery",
)

class HassBoxMapSensor(RestoreEntity, SensorEntity):

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, manager: TrackerManager, device: str, description: SensorEntityDescription) -> None:
        self._manager = manager
        self._device = device
        self.entity_description = description
        self._fix = manager.fixes.get(device)
        self._online = manager.is_online(device)
        self._restored_value: StateType = None
        self._attr_unique_id = f"{DOMAIN}_{device}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device)})

    async def async_added_to_hass(self) -> None:

        await super().async_added_to_hass()
        if self._fix is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        value = self._parse_restored_value(last_state.state)
        if value is None:
            return
        self._restored_value = value
        self._online = True
        self.async_write_ha_state()

    def _parse_restored_value(self, raw: str) -> StateType:

        if self.entity_description.key == "device_time":
            return dt_util.parse_datetime(raw)
        if self.entity_description.device_class == SensorDeviceClass.ENUM:
            options = self.entity_description.options or []
            return raw if raw in options else None
        try:
            return float(raw)
        except ValueError:
            return None

    @callback
    def set_fix(self, fix: LocationFix) -> None:
        self._fix = fix
        self._online = True

    @callback
    def set_online(self, online: bool) -> None:
        self._online = online

    @property
    def available(self) -> bool:

        return True

    @property
    def native_value(self) -> StateType:
        if self._fix is None:
            return self._restored_value
        key = self.entity_description.key
        if key == "latitude":
            return self._fix.latitude
        if key == "longitude":
            return self._fix.longitude
        if key == "speed":
            return self._fix.speed_kmh
        if key == "altitude":
            return self._fix.altitude
        if key == "direction":
            return self._fix.direction
        if key == "device_time":
            return self._fix.device_time
        if key == "gps_fix":
            return "定位" if self._fix.gps_fix else "未定位"
        if key == "moving":
            return "移动" if self._fix.moving else "静止"
        if key == "battery":
            value = self._fix.extras.get("battery_percent")
            return int(value) if value is not None else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._fix is None:
            return None
        attrs: dict[str, Any] = {
            "online": self._online,
            "device_time": self._fix.device_time.isoformat(),
        }
        if self.entity_description.key == "battery":

            attrs["power_source"] = self._fix.extras.get("power_source")
        return attrs

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:

    manager: TrackerManager = hass.data[DOMAIN][entry.entry_id][DATA_TRACKERS]
    manager.register_add_sensors(async_add_entities)

    devices = set(manager.known_devices())

    new_entities: list[HassBoxMapSensor] = []
    for device in sorted(devices):
        if device not in manager.sensors:
            sensors = [
                HassBoxMapSensor(manager, device, desc) for desc in SENSOR_DESCRIPTIONS
            ]
            manager.sensors[device] = sensors
            new_entities.extend(sensors)
    if new_entities:
        async_add_entities(new_entities)
        _LOGGER.debug("sensor 平台就绪，实体 %d 个", len(new_entities))

    await async_setup_fence_entities(
        hass,
        hass.data[DOMAIN][entry.entry_id][DATA_STORE],
        manager,
        async_add_entities,
        entry,
    )
