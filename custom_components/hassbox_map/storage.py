from __future__ import annotations

from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DEVICES_STORAGE_KEY, STORAGE_KEY, STORAGE_VERSION

Geofence = dict[str, Any]
Device = dict[str, Any]

class GeofenceStore:

    def __init__(self, hass: HomeAssistant) -> None:

        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._fences: dict[str, Geofence] = {}

    async def async_load(self) -> None:

        data = await self._store.async_load()
        for fence in (data or {}).get("geofences", []):
            fid = fence.get("id")
            if not fid:
                continue
            if "polygons" not in fence and fence.get("polygon"):
                fence["polygons"] = [fence["polygon"]]
            fence.pop("polygon", None)

            fence.pop("name", None)
            self._fences[fid] = fence

    @callback
    def list(self) -> list[Geofence]:

        return list(self._fences.values())

    @callback
    def get(self, fence_id: str) -> Geofence | None:

        return self._fences.get(fence_id)

    async def async_save(self, data: Geofence) -> Geofence:

        fence_id = data.get("id") or f"fence_{uuid4().hex[:12]}"
        rings = data.get("polygons")
        if rings is None and data.get("polygon"):
            rings = [data["polygon"]]

        fence: Geofence = {
            "id": fence_id,

            "polygons": [
                [
                    {"lat": float(p["lat"]), "lng": float(p["lng"])}
                    for p in ring
                ]
                for ring in (rings or [])
            ],
            "enabled": bool(data.get("enabled", True)),
        }
        self._fences[fence_id] = fence
        await self._persist()
        return fence

    async def async_delete(self, fence_id: str) -> bool:

        if fence_id not in self._fences:
            return False
        del self._fences[fence_id]
        await self._persist()
        return True

    async def _persist(self) -> None:

        await self._store.async_save({"geofences": list(self._fences.values())})

class DeviceStore:

    def __init__(self, hass: HomeAssistant) -> None:

        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, DEVICES_STORAGE_KEY
        )
        self._devices: dict[str, Device] = {}

    async def async_load(self) -> None:

        data = await self._store.async_load()
        for item in (data or {}).get("devices", []):
            sn = (item or {}).get("sn")
            if sn:
                self._devices[sn] = {"sn": sn, "name": item.get("name") or ""}

    @callback
    def list(self) -> list[Device]:

        return list(self._devices.values())

    @callback
    def sns(self) -> list[str]:

        return list(self._devices)

    async def async_remember(self, devices: list[Device]) -> bool:

        changed = False
        for item in devices or []:
            sn = (item or {}).get("sn")
            if not sn:
                continue
            name = item.get("name")
            entry: Device = {
                "sn": sn,
                "name": str(name) if name else self._devices.get(sn, {}).get("name", ""),
            }
            if self._devices.get(sn) != entry:
                self._devices[sn] = entry
                changed = True
        if changed:
            await self._persist()
        return changed

    async def async_forget(self, sns: list[str]) -> None:

        removed = False
        for sn in sns:
            if self._devices.pop(sn, None) is not None:
                removed = True
        if removed:
            await self._persist()

    async def _persist(self) -> None:

        await self._store.async_save({"devices": list(self._devices.values())})
