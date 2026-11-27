from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util.location import distance as ha_distance

from .const import DATA_FENCE_MANAGER, DATA_FENCES_SIGNAL, DOMAIN, EVENT_FENCES_CHANGED
from .device_tracker import TrackerManager
from .storage import GeofenceStore

_LOGGER = logging.getLogger(__name__)

_TRACKER_MARK = "device_time"

FENCE_DEVICE_IDENTIFIER = "fences"
FENCE_DEVICE_NAME = "围栏"

ENTER_TOLERANCE_M = 12.0
EXIT_TOLERANCE_M = 24.0

_BBOX_PAD_DEG = 0.0005

_LAT_M_PER_DEG = 111_320.0

def _fence_unique_id(fence_id: str) -> str:
    return f"{DOMAIN}_fence_{fence_id}"

def _fence_device_info() -> DeviceInfo:

    return DeviceInfo(
        identifiers={(DOMAIN, FENCE_DEVICE_IDENTIFIER)},
        name=FENCE_DEVICE_NAME,
        manufacturer="HassBox",
        model="地理围栏",
        entry_type=DeviceEntryType.SERVICE,
    )

class FenceGeometry:

    __slots__ = (
        "rings",
        "min_lat",
        "max_lat",
        "min_lng",
        "max_lng",
        "valid",
        "radius",
    )

    def __init__(self, polygons: list[list[dict[str, Any]]]) -> None:
        rings: list[list[tuple[float, float]]] = []
        for ring in polygons or []:
            points: list[tuple[float, float]] = []
            for point in ring:
                try:
                    points.append((float(point["lat"]), float(point["lng"])))
                except (KeyError, TypeError, ValueError):
                    continue
            if len(points) >= 3:
                rings.append(points)
        self.rings = rings
        self.valid = bool(rings)
        lats = [p[0] for ring in rings for p in ring]
        lngs = [p[1] for ring in rings for p in ring]
        self.min_lat = min(lats) if lats else 0.0
        self.max_lat = max(lats) if lats else 0.0
        self.min_lng = min(lngs) if lngs else 0.0
        self.max_lng = max(lngs) if lngs else 0.0
        self.radius = self._compute_radius() if rings else 0.0

    def _compute_radius(self) -> float:

        lat_m = (self.max_lat - self.min_lat) * _LAT_M_PER_DEG
        mid_lat = (self.min_lat + self.max_lat) / 2
        lng_m = (
            (self.max_lng - self.min_lng)
            * _LAT_M_PER_DEG
            * max(math.cos(math.radians(mid_lat)), 1e-6)
        )
        return math.hypot(lat_m, lng_m) / 2

    def _in_bbox(self, lat: float, lng: float) -> bool:
        return (
            self.min_lat - _BBOX_PAD_DEG <= lat <= self.max_lat + _BBOX_PAD_DEG
            and self.min_lng - _BBOX_PAD_DEG <= lng <= self.max_lng + _BBOX_PAD_DEG
        )

    def contains(self, lat: float, lng: float) -> bool:

        return any(_point_in_ring(lat, lng, ring) for ring in self.rings)

    def distance_m(self, lat: float, lng: float) -> float:

        scale = _LAT_M_PER_DEG * max(math.cos(math.radians(lat)), 1e-6)
        px = lng * scale
        py = lat * _LAT_M_PER_DEG
        best = math.inf
        for ring in self.rings:
            count = len(ring)
            for i in range(count):
                a = ring[i]
                b = ring[(i + 1) % count]
                best = min(
                    best,
                    _segment_distance(
                        px,
                        py,
                        a[1] * scale,
                        a[0] * _LAT_M_PER_DEG,
                        b[1] * scale,
                        b[0] * _LAT_M_PER_DEG,
                    ),
                )
        return best

    def inside(self, lat: float, lng: float, previous: bool) -> bool:

        if not self.valid or not self._in_bbox(lat, lng):
            return False
        if self.contains(lat, lng):
            return True
        tolerance = EXIT_TOLERANCE_M if previous else ENTER_TOLERANCE_M
        return self.distance_m(lat, lng) <= tolerance

class FenceCircle:

    __slots__ = ("lat", "lng", "radius", "valid")

    def __init__(self, lat: float, lng: float, radius: float) -> None:
        self.lat = lat
        self.lng = lng
        self.radius = radius
        self.valid = radius > 0

    def inside(self, lat: float, lng: float, _previous: bool = False) -> bool:

        if not self.valid:
            return False
        return ha_distance(self.lat, self.lng, lat, lng) <= self.radius

def _point_in_ring(lat: float, lng: float, ring: list[tuple[float, float]]) -> bool:

    inside = False
    count = len(ring)
    j = count - 1
    for i in range(count):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat):
            cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < cross:
                inside = not inside
        j = i
    return inside

def _segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:

    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

def _state_position(state: State | None) -> tuple[float, float] | None:

    if state is None:
        return None
    if state.domain != "person" and _TRACKER_MARK not in state.attributes:
        return None
    lat = state.attributes.get("latitude")
    lng = state.attributes.get("longitude")
    if lat is None or lng is None:
        return None
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None

def _is_own_tracker(state: State | None) -> bool:

    return bool(
        state is not None
        and state.entity_id.startswith("device_tracker.")
        and _TRACKER_MARK in state.attributes
    )

def _is_tracked(state: State | None) -> bool:

    return bool(state is not None and (state.domain == "person" or _is_own_tracker(state)))

def _zone_fence_id(entity_id: str) -> str:

    return entity_id.replace(".", "_", 1)

def _zone_signature(state: State) -> tuple:

    attrs = state.attributes
    return (
        state.name,
        attrs.get("latitude"),
        attrs.get("longitude"),
        attrs.get("radius"),
        attrs.get("passive"),
    )

class HassBoxFenceSensor(SensorEntity):

    _attr_should_poll = False
    _attr_has_entity_name = True

    @property
    def name(self) -> str:

        return self._manager.fence_name(self._fence_id) or FENCE_DEVICE_NAME

    def __init__(self, manager: "FenceEntityManager", fence_id: str) -> None:
        self._manager = manager
        self._fence_id = fence_id

        self._last_sig: tuple | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return _fence_device_info()

    @property
    def icon(self) -> str:

        return (
            "mdi:map-marker-radius"
            if self._manager.is_zone(self._fence_id)
            else "mdi:vector-polygon"
        )

    @property
    def unique_id(self) -> str:
        return _fence_unique_id(self._fence_id)

    @property
    def available(self) -> bool:
        return self._manager.fence(self._fence_id) is not None

    @property
    def native_value(self) -> int:

        return len(self._manager.devices(self._fence_id)) + len(
            self._manager.persons(self._fence_id)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        fence = self._manager.fence(self._fence_id) or {}
        devices = self._manager.devices(self._fence_id)
        persons = self._manager.persons(self._fence_id)
        attrs: dict[str, Any] = {
            "fence_id": self._fence_id,
            "fence_name": self._manager.fence_name(self._fence_id),
            "kind": fence.get("kind", "polygon"),
            "enabled": bool(fence.get("enabled", True)),

            "radius": self._manager.fence_radius(self._fence_id),
            "passive": False,
            "editable": False,

            "devices": devices,
            "device_names": self._manager.names(devices),
            "device_count": len(devices),

            "persons": persons,
            "person_names": self._manager.names(persons),
            "person_count": len(persons),
            "total_count": len(devices) + len(persons),
        }
        if self._manager.is_zone(self._fence_id):
            attrs["zone_entity_id"] = fence.get("zone_entity_id")
        else:
            attrs["polygons"] = fence.get("polygons") or []
        return attrs

    @callback
    def async_refresh(self, force: bool = False) -> None:

        fence = self._manager.fence(self._fence_id)
        sig: tuple = (
            tuple(self._manager.devices(self._fence_id)),
            tuple(self._manager.persons(self._fence_id)),
            bool(fence and fence.get("enabled", True)),
        )
        if sig == self._last_sig and not force:
            return
        self._last_sig = sig
        self.async_write_ha_state()

class FenceEntityManager:

    def __init__(
        self,
        hass: HomeAssistant,
        store: GeofenceStore,
        trackers: TrackerManager,
        async_add_entities,
        entry_id: str,
    ) -> None:
        self._hass = hass
        self._store = store
        self._trackers = trackers
        self._async_add_entities = async_add_entities
        self._entry_id = entry_id
        self._entities: dict[str, HassBoxFenceSensor] = {}

        self._geoms: dict[str, FenceGeometry | FenceCircle] = {}

        self._zones: dict[str, dict[str, Any]] = {}

        self._inside: dict[str, set[str]] = {}

        self._pending_names: dict[str, str] = {}

    @callback
    def async_start(self) -> list[Any]:

        self._refresh_zones()
        self._cleanup_legacy_entities()
        self._add_missing()
        self._rebuild()

        if self._hass.is_running:
            self._migrate_fence_names()
            self._sync_fence_devices()
        else:
            self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._handle_started
            )
        unsub_bus = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_changed
        )
        unsub_signal = async_dispatcher_connect(
            self._hass, DATA_FENCES_SIGNAL, self._handle_fences_changed
        )
        unsub_entity = self._hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_entity_updated
        )
        return [unsub_bus, unsub_signal, unsub_entity]

    @callback
    def _handle_started(self, _event: Event) -> None:

        self._migrate_fence_names()
        self._sync_fence_devices()

    @callback
    def fence(self, fence_id: str) -> dict[str, Any] | None:

        zone = self._zones.get(fence_id)
        if zone is not None:
            return zone
        return self._store.get(fence_id)

    @callback
    def is_zone(self, fence_id: str) -> bool:

        return fence_id in self._zones

    @callback
    def _valid_fence_ids(self) -> set[str]:

        ids = {fence.get("id") for fence in self._store.list() if fence.get("id")}
        ids |= set(self._zones)
        return ids

    @callback
    def devices(self, fence_id: str) -> list[str]:

        return sorted(
            entity_id
            for entity_id in self._inside.get(fence_id, set())
            if entity_id.startswith("device_tracker.")
        )

    @callback
    def persons(self, fence_id: str) -> list[str]:

        return sorted(
            entity_id
            for entity_id in self._inside.get(fence_id, set())
            if entity_id.startswith("person.")
        )

    @callback
    def fence_radius(self, fence_id: str) -> float:

        geom = self._geoms.get(fence_id)
        return round(geom.radius, 1) if geom is not None else 0.0

    @callback
    def fence_name(self, fence_id: str) -> str:

        entity_registry = er.async_get(self._hass)
        entity_id = entity_registry.async_get_entity_id(
            "sensor", DOMAIN, _fence_unique_id(fence_id)
        )
        if entity_id is not None:
            entry = entity_registry.async_get(entity_id)
            if entry is not None:
                name = (entry.name or entry.original_name or "").strip()
                if name:
                    self._pending_names.pop(fence_id, None)
                    return name
        zone = self._zones.get(fence_id)
        if zone is not None:
            return str(zone.get("name") or fence_id)
        return self._pending_names.get(fence_id, "")

    @callback
    def async_set_fence_name(self, fence_id: str, name: str) -> None:

        name = name.strip()
        if not fence_id or not name:
            return
        self._pending_names[fence_id] = name
        entity_registry = er.async_get(self._hass)
        entity_id = entity_registry.async_get_entity_id(
            "sensor", DOMAIN, _fence_unique_id(fence_id)
        )
        if entity_id is None:
            return
        entity_registry.async_update_entity(entity_id, name=name)
        entity = self._entities.get(fence_id)
        if entity is not None:
            entity.async_write_ha_state()

    @callback
    def names(self, entity_ids: list[str]) -> list[str]:

        names: list[str] = []
        for entity_id in entity_ids:
            state = self._hass.states.get(entity_id)
            names.append(state.name if state is not None else entity_id)
        return names

    @callback
    def _refresh_zones(self) -> None:

        zones: dict[str, dict[str, Any]] = {}
        for state in self._hass.states.async_all("zone"):
            fid = _zone_fence_id(state.entity_id)
            zones[fid] = {
                "id": fid,
                "kind": "zone",
                "zone_entity_id": state.entity_id,
                "name": state.name,
                "polygons": [],
                "enabled": True,
                "latitude": state.attributes.get("latitude"),
                "longitude": state.attributes.get("longitude"),
                "radius": state.attributes.get("radius"),
            }
        self._zones = zones

    @callback
    def _cleanup_legacy_entities(self) -> None:

        registry = er.async_get(self._hass)
        for entry in list(registry.entities.values()):
            unique_id = entry.unique_id or ""
            if not unique_id.startswith(f"{DOMAIN}_fence_"):
                continue
            if unique_id.endswith(("_devices", "_persons", "_persons_count")):
                registry.async_remove(entry.entity_id)
                _LOGGER.debug("[fence] 清理旧围栏实体 %s", entry.entity_id)

    @callback
    def _add_missing(self) -> None:

        new_entities: list[SensorEntity] = []
        for fid in sorted(self._valid_fence_ids()):
            if fid in self._entities:
                continue
            entity = HassBoxFenceSensor(self, fid)
            self._entities[fid] = entity
            new_entities.append(entity)
        if new_entities:
            self._async_add_entities(new_entities)
            _LOGGER.debug("[fence] 新建围栏实体 %d 个", len(new_entities))

    @callback
    def _prune_missing(self, existing: set[str]) -> None:

        for fid, entity in list(self._entities.items()):
            if fid in existing:
                continue
            self._entities.pop(fid, None)
            self._hass.async_create_task(entity.async_remove(force_remove=True))
            _LOGGER.debug("[fence] 移除围栏实体 %s", fid)

    @callback
    def _migrate_fence_names(self) -> None:

        device_registry = dr.async_get(self._hass)
        entity_registry = er.async_get(self._hass)
        for fid in self._valid_fence_ids():
            devices = device_registry.async_get_devices(identifiers={(DOMAIN, fid)})
            if not devices:
                continue
            name = (devices[0].name_by_user or devices[0].name or "").strip()
            if not name:
                continue
            entity_id = entity_registry.async_get_entity_id(
                "sensor", DOMAIN, _fence_unique_id(fid)
            )
            if entity_id is None:
                continue
            entry = entity_registry.async_get(entity_id)
            if entry is None or entry.name:

                continue
            entity_registry.async_update_entity(entity_id, name=name)
            _LOGGER.debug("[fence] 迁移围栏名称 %s -> %s", fid, name)

    @callback
    def _sync_fence_devices(self) -> None:

        registry = dr.async_get(self._hass)
        valid = self._valid_fence_ids()

        entity_registry = er.async_get(self._hass)
        prefix = f"{DOMAIN}_fence_"
        for entry in list(entity_registry.entities.values()):
            unique_id = entry.unique_id or ""
            if not unique_id.startswith(prefix):
                continue
            if unique_id[len(prefix) :] not in valid:
                entity_registry.async_remove(entry.entity_id)
                _LOGGER.debug("[fence] 清理失效围栏实体 %s", entry.entity_id)

        shared = registry.async_get_devices(
            identifiers={(DOMAIN, FENCE_DEVICE_IDENTIFIER)}
        )
        if shared:
            device = shared[0]
            updates: dict[str, Any] = {}
            if device.manufacturer != "HassBox":
                updates["manufacturer"] = "HassBox"
            if device.model != "地理围栏":
                updates["model"] = "地理围栏"
            if updates:
                registry.async_update_device(device.id, **updates)
        else:
            registry.async_get_or_create(
                config_entry_id=self._entry_id,
                identifiers={(DOMAIN, FENCE_DEVICE_IDENTIFIER)},
                name=FENCE_DEVICE_NAME,
                manufacturer="HassBox",
                model="地理围栏",
                entry_type=DeviceEntryType.SERVICE,
            )
            _LOGGER.debug("[fence] 创建共用围栏设备 %s", FENCE_DEVICE_NAME)

        for fid, info in self._zones.items():
            name = str(info.get("name") or "").strip()
            if name:
                self.async_set_fence_name(fid, name)

        tracker_devices = set(self._trackers.known_devices())
        legacy_ids = {
            entry.device_id
            for entry in entity_registry.entities.values()
            if (entry.unique_id or "").startswith(prefix) and entry.device_id
        }
        for fid in valid:
            device = registry.async_get_device_by_identifier(
                (DOMAIN, fid), self._entry_id
            )
            if device is not None and fid not in tracker_devices:
                legacy_ids.add(device.id)
        for device_id in legacy_ids:
            device = registry.async_get(device_id)
            if device is None:
                continue

            if (DOMAIN, FENCE_DEVICE_IDENTIFIER) in device.identifiers:
                continue
            fence_id = next(
                (
                    str(ident)
                    for domain, ident in device.identifiers
                    if domain == DOMAIN
                ),
                device_id,
            )
            self._hass.async_create_task(
                self._async_remove_device_entry(device.id, fence_id)
            )

    async def _async_remove_device_entry(self, device_id: str, fence_id: str) -> None:

        registry = dr.async_get(self._hass)
        if registry.async_get(device_id) is not None:
            registry.async_remove_device(device_id)
            _LOGGER.debug("[fence] 移除旧版围栏设备 %s", fence_id)

    @callback
    def _handle_fences_changed(self) -> None:

        self._add_missing()
        self._prune_missing(self._valid_fence_ids())
        self._sync_fence_devices()
        self._rebuild(force=True)

    @callback
    def _handle_zones_changed(self) -> None:

        self._refresh_zones()

        for fid, info in self._zones.items():
            name = str(info.get("name") or "").strip()
            if name:
                self.async_set_fence_name(fid, name)
        self._add_missing()
        self._prune_missing(self._valid_fence_ids())
        self._sync_fence_devices()
        self._rebuild(force=True)
        self._hass.bus.async_fire(EVENT_FENCES_CHANGED)

    @callback
    def _handle_entity_updated(self, event: Event) -> None:

        if event.data.get("action") != "update":
            return
        changes = event.data.get("changes") or {}
        if "name" not in changes and "original_name" not in changes:
            return
        entity_id = event.data.get("entity_id")
        entry = er.async_get(self._hass).async_get(entity_id) if entity_id else None
        if entry is None:
            return
        prefix = f"{DOMAIN}_fence_"
        unique_id = entry.unique_id or ""
        if not unique_id.startswith(prefix):
            return
        fence_id = unique_id[len(prefix) :]
        if fence_id not in self._valid_fence_ids():
            return

        self._pending_names.pop(fence_id, None)
        entity = self._entities.get(fence_id)
        if entity is not None:
            entity.async_refresh(force=True)
        self._hass.bus.async_fire(EVENT_FENCES_CHANGED)

    @callback
    def _handle_state_changed(self, event: Event) -> None:

        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if (new_state is not None and new_state.domain == "zone") or (
            old_state is not None and old_state.domain == "zone"
        ):
            if (
                new_state is None
                or old_state is None
                or _zone_signature(new_state) != _zone_signature(old_state)
            ):
                self._handle_zones_changed()
            return
        if not _is_tracked(new_state) and not _is_tracked(old_state):
            return
        entity_id = (new_state or old_state).entity_id
        changed = self._refresh_membership(
            entity_id, _state_position(self._hass.states.get(entity_id))
        )

        if _is_own_tracker(new_state) or _is_own_tracker(old_state):
            self._sync_device_attr(entity_id)
        if not changed:
            return
        for fid in changed:
            entity = self._entities.get(fid)
            if entity is not None:
                entity.async_refresh()

        self._hass.bus.async_fire(EVENT_FENCES_CHANGED)

    @callback
    def _rebuild(self, force: bool = False) -> None:

        self._geoms = {}
        for fence in self._store.list():
            fid = fence.get("id")
            if not fid or not fence.get("enabled", True):
                continue
            self._geoms[fid] = FenceGeometry(fence.get("polygons") or [])

        for fid, info in self._zones.items():
            try:
                lat = float(info["latitude"])
                lng = float(info["longitude"])
                radius = float(info["radius"])
            except (KeyError, TypeError, ValueError):
                continue
            self._geoms[fid] = FenceCircle(lat, lng, radius)
        self._inside = {fid: set() for fid in self._geoms}

        trackers: list[str] = []
        for state in self._hass.states.async_all("device_tracker"):
            if not _is_own_tracker(state):
                continue
            trackers.append(state.entity_id)
            self._refresh_membership(state.entity_id, _state_position(state))

        for state in self._hass.states.async_all("person"):
            if _state_position(state) is None:
                continue
            self._refresh_membership(state.entity_id, _state_position(state))

        for entity_id in trackers:
            self._sync_device_attr(entity_id)
        for entity in self._entities.values():
            entity.async_refresh(force)

    @callback
    def _refresh_membership(
        self, entity_id: str, position: tuple[float, float] | None
    ) -> set[str]:

        changed: set[str] = set()
        for fid, geom in self._geoms.items():
            members = self._inside.setdefault(fid, set())
            previous = entity_id in members
            inside = (
                False
                if position is None
                else geom.inside(position[0], position[1], previous)
            )
            if inside and not previous:
                members.add(entity_id)
                changed.add(fid)
            elif not inside and previous:
                members.discard(entity_id)
                changed.add(fid)
        return changed

    @callback
    def _fence_entity_id(self, fence_id: str) -> str | None:

        entity = self._entities.get(fence_id)
        if entity is not None and entity.entity_id:
            return entity.entity_id
        registry = er.async_get(self._hass)
        return registry.async_get_entity_id(
            "sensor", DOMAIN, _fence_unique_id(fence_id)
        )

    @callback
    def _sync_device_attr(self, entity_id: str) -> None:

        tracker = next(
            (
                entity
                for entity in self._trackers.entities.values()
                if entity.entity_id == entity_id
            ),
            None,
        )
        if tracker is None:
            return
        fence_entity_ids: list[str] = []
        for fid in sorted(self._inside):
            if self.is_zone(fid):

                continue
            if entity_id not in self._inside[fid]:
                continue
            fence_entity_id = self._fence_entity_id(fid)
            if fence_entity_id is not None:
                fence_entity_ids.append(fence_entity_id)
        tracker.set_in_zones(fence_entity_ids)

async def async_setup_fence_entities(
    hass: HomeAssistant,
    store: GeofenceStore,
    trackers: TrackerManager,
    async_add_entities,
    entry,
) -> None:

    manager = FenceEntityManager(
        hass, store, trackers, async_add_entities, entry.entry_id
    )

    hass.data[DOMAIN][entry.entry_id][DATA_FENCE_MANAGER] = manager
    for unsub in manager.async_start():
        entry.async_on_unload(unsub)
