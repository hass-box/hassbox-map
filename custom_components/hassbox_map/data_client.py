from __future__ import annotations

import logging

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .utils.store import async_save_to_store

_LOGGER = logging.getLogger(__name__)

base_url = "https://hassbox.cn/api/public/"
app_id = "gh_07ec63f43481"

class HassBoxDataClient:

    def __init__(self, hass, config=None):
        self.hass = hass
        self.session = async_get_clientsession(hass, False)
        self.token = None
        if config is not None and "token" in config:
            self.token = config["token"]

    async def __fetch(self, api, data, headers=None):
        data["appId"] = app_id
        async with self.session.post(base_url + api, json=data, headers=headers) as response:
            return await response.json()

    async def get_qrcode(self):

        result = await self.__fetch("map/getQRCode", {"token": self.token})
        if "token" in result:
            self.token = result["token"]
        return result

    async def check_state(self):

        result = await self.__fetch("map/checkState", {"token": self.token})
        if "token" in result:
            self.token = result["token"]

            service_id = result.get("serviceId") or result.get("service_id") or ""
            self.service_id = service_id
            await async_save_to_store(
                self.hass, "hassbox_map.config", {
                    "token": result["token"],
                    "service_id": service_id,
                }
            )
        return result

    async def get_devices(self):

        result = await self.__fetch("map/getDevices", {"token": self.token})
        if isinstance(result, list):
            return result
        return []

    async def unbind(self, service_id):

        if not self.token:
            return {"errcode": 401, "errmsg": "当前未绑定账号"}
        return await self.__fetch(
            "integration/resetUserActivation",
            {"serviceId": service_id},
            headers={"Authorization": self.token},
        )

    async def bind_device(self, service_id, sn, name):

        if not self.token:
            return {"errcode": 401, "errmsg": "当前未绑定账号"}
        return await self.__fetch(
            "integration/map/bindDevice",
            {"serviceId": service_id, "sn": sn, "name": name},
            headers={"Authorization": self.token},
        )

    async def unbind_device(self, service_id, sn):

        if not self.token:
            return {"errcode": 401, "errmsg": "当前未绑定账号"}
        return await self.__fetch(
            "integration/map/unbindDevice",
            {"serviceId": service_id, "sn": sn},
            headers={"Authorization": self.token},
        )

    async def report_location(self, sn, fix):

        result = await self.__fetch(
            "map/reportLocation",
            {
                "token": self.token,
                "sn": sn,
                "latitude": fix.latitude,
                "longitude": fix.longitude,
                "speed": fix.speed_kmh,
                "direction": fix.direction,
                "deviceTime": fix.device_time.isoformat(),
            },
        )
        return result
