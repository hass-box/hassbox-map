from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

import aiohttp
from homeassistant.core import callback

from .jt808 import DROP_NO_FIX, LocationFix, parse_frame

_LOGGER = logging.getLogger(__name__)

WS_HEARTBEAT = 30

RECONNECT_MIN = 1
RECONNECT_MAX = 30
RECONNECT_JITTER_MIN = 0.5
RECONNECT_JITTER_MAX = 1.0

REFRESH_KEY = "refresh:*"

MSG_AUTHENTICATED = "authenticated"
MSG_LOCATION = "location"
MSG_ERROR = "error"
MSG_COMMAND_ACK = "command_ack"

COMMAND_ACK_TIMEOUT = 10

class BridgeError(Exception):
    """bridge 上行指令失败（未连接、服务端报错或等待回执超时）。"""

LocationCallback = Callable[[str, LocationFix], None]
UnlocatedCallback = Callable[[str, LocationFix], None]
StateCallback = Callable[[str, bool], None]
DevicesCallback = Callable[[list[dict[str, Any]]], None]

class BridgeManager:

    def __init__(
        self,
        hass,
        ws_url: str,
        token: str,
        service_id: str,
        on_location: LocationCallback,
        on_state: StateCallback,
        on_devices: DevicesCallback,
        on_unlocated: UnlocatedCallback | None = None,
    ) -> None:
        self.hass = hass
        self.ws_url = ws_url
        self.token = token
        self.service_id = service_id
        self.on_location = on_location
        self.on_state = on_state
        self.on_devices = on_devices
        self.on_unlocated = on_unlocated

        self.devices: list[str] = []
        self.device_names: dict[str, str] = {}
        self.online = False

        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}

        self._reconnect_count = 0
        self._frames_received = 0
        self._loc_received = 0
        self._loc_valid = 0
        self._loc_dropped = 0
        self._consecutive_dropped = 0
        self._last_drop_reason = ""

    @property
    def started(self) -> bool:

        return self._task is not None

    def start(self) -> None:

        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="hassbox_map_bridge"
        )
        _LOGGER.debug(
            "[bridge] 连接任务已启动 ws=%s service=%s",
            self.ws_url, self.service_id,
        )

    async def stop(self) -> None:

        _LOGGER.debug(
            "bridge 正在停止（累计重连 %d 次，收帧 %d，有效定位 %d）",
            self._reconnect_count, self._frames_received, self._loc_valid,
        )
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        self._set_all_offline()
        _LOGGER.debug("bridge 已停止")

    async def _run(self) -> None:

        backoff = RECONNECT_MIN
        while True:
            try:
                authed = await self._connect_and_read()
                if authed:
                    backoff = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "bridge 连接异常: %s: %r",
                    type(err).__name__, err, exc_info=True,
                )
            self._set_all_offline()
            self._reconnect_count += 1
            delay = backoff * random.uniform(
                RECONNECT_JITTER_MIN, RECONNECT_JITTER_MAX
            )
            _LOGGER.debug(
                "bridge 断开，第 %d 次重连，%.1fs 后重试",
                self._reconnect_count, delay,
            )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _connect_and_read(self) -> bool:

        if not self.token:
            _LOGGER.warning("bridge 缺少 token，无法鉴权（%ds 后重试）", RECONNECT_MAX)
            await asyncio.sleep(RECONNECT_MAX)
            return False

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        _LOGGER.debug("bridge 正在建立连接 %s", self.ws_url)
        try:
            ws = await self._session.ws_connect(
                self.ws_url, heartbeat=WS_HEARTBEAT,
            )
        except aiohttp.ClientResponseError as err:

            _LOGGER.warning(
                "bridge ws_connect HTTP %d %s",
                err.status, err.message, exc_info=True,
            )
            raise
        except aiohttp.ClientConnectorError as err:

            _LOGGER.warning(
                "bridge ws_connect 连接失败: %r", err, exc_info=True,
            )
            raise
        except Exception:
            _LOGGER.warning("bridge ws_connect 未知异常", exc_info=True)
            raise

        authed = False
        _LOGGER.debug("[bridge] 已连接，发送鉴权 service=%s", self.service_id)
        try:
            await ws.send_json(
                {"action": "auth", "token": self.token, "service_id": self.service_id}
            )
            while True:
                try:
                    msg = await ws.receive()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    _LOGGER.warning(
                        "bridge ws.receive 异常: %s: %r",
                        type(err).__name__, err, exc_info=True,
                    )
                    break

                self._frames_received += 1

                if msg.type == aiohttp.WSMsgType.TEXT:
                    if self._handle_message(msg.data):
                        authed = True
                        self._ws = ws
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    _LOGGER.debug("bridge 收到 BINARY 帧 (%d bytes) 忽略", len(msg.data))
                elif msg.type == aiohttp.WSMsgType.PING:
                    pass
                elif msg.type == aiohttp.WSMsgType.PONG:
                    pass
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    _LOGGER.debug(
                        "bridge 收到 CLOSE frame (code=%s, message=%s)",
                        getattr(msg, "data", None), getattr(msg, "extra", None),
                    )
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.debug("bridge 连接已关闭")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning(
                        "bridge 收到 ERROR frame (exception=%s)",
                        getattr(msg, "exception", None),
                        exc_info=getattr(msg, "exception", None) is not None,
                    )
                    break
        finally:
            self._ws = None
            self._fail_pending(BridgeError("bridge 连接已断开"))
            close_code = ws.close_code if ws.closed else None
            _LOGGER.debug(
                "bridge 会话结束 (close_code=%s, 本次收帧 %d, 有效定位 %d)",
                close_code, self._frames_received, self._loc_valid,
            )
            try:
                await ws.close()
            except Exception:
                pass
        return authed

    def _handle_message(self, raw: str) -> bool:

        _LOGGER.debug("[bridge] handle_message %s", raw)

        try:
            msg: dict[str, Any] = json.loads(raw)
        except ValueError:
            _LOGGER.warning("bridge 收到非 JSON TEXT 帧: %r", raw[:200])
            self._loc_dropped += 1
            return False

        msg_type = msg.get("type", "<missing>")

        if msg_type == MSG_AUTHENTICATED:
            devices = msg.get("devices") or []
            sns: list[str] = []
            names: dict[str, str] = {}
            for item in devices:
                sn = (item or {}).get("sn")
                if not sn:
                    continue
                sns.append(sn)
                name = item.get("name")
                if name:
                    names[sn] = str(name)

            sns = list(dict.fromkeys(sns))
            self.devices = sns
            self.device_names = names
            self.online = True
            self._consecutive_dropped = 0
            _LOGGER.debug(
                "[bridge] 鉴权成功 service=%s 设备 %d 台: %s",
                msg.get("service_id") or self.service_id,
                len(sns), ", ".join(sns) or "(空)",
            )

            try:
                self.on_devices(devices)
            except Exception:
                _LOGGER.warning("bridge on_devices 回调异常", exc_info=True)
            for sn in sns:
                self._on_state(sn, True)

            fut = self._pending.pop(REFRESH_KEY, None)
            if fut is not None and not fut.done():
                fut.set_result(True)
            return True

        if msg_type == MSG_COMMAND_ACK:
            action = str(msg.get("action") or "")
            device = str(msg.get("device") or "")
            fut = self._pending.pop(f"{action}:{device}", None)
            _LOGGER.debug(
                "[bridge] 指令回执 action=%s device=%s ok=%s",
                action, device, msg.get("ok"),
            )
            if fut is not None and not fut.done():
                if msg.get("ok"):
                    fut.set_result(True)
                else:
                    fut.set_exception(BridgeError("指令未入队"))
            return False

        if msg_type == MSG_LOCATION:
            self._loc_received += 1
            device = msg.get("device") or ""
            data = msg.get("data") or ""
            fix, reason = parse_frame(data)
            if fix is None:
                self._loc_dropped += 1
                self._consecutive_dropped += 1
                self._last_drop_reason = reason or "未知"
                _LOGGER.debug(
                    "[bridge] 定位帧丢弃 (device=%s, reason=%s, hex_len=%d, hex=%s)",
                    device, self._last_drop_reason, len(data), data,
                )
                return False

            if reason == DROP_NO_FIX:
                self._consecutive_dropped = 0
                _LOGGER.debug(
                    "[bridge] 未定位帧 (device=%s, %s, time=%s)，仅刷新上报时间",
                    device, reason, fix.device_time.isoformat(),
                )
                if self.on_unlocated is not None:
                    try:
                        self.on_unlocated(device, fix)
                    except Exception:
                        _LOGGER.warning(
                            "bridge on_unlocated 回调异常 device=%s",
                            device, exc_info=True,
                        )
                return False
            self._consecutive_dropped = 0
            self._loc_valid += 1
            try:
                self.on_location(device, fix)
            except Exception:
                _LOGGER.warning(
                    "bridge on_location 回调异常 device=%s", device, exc_info=True,
                )
            _LOGGER.debug(
                "[bridge] 定位 device=%s lat=%.6f lng=%.6f speed=%.1fkmh dir=%d alt=%sm time=%s",
                device, fix.latitude, fix.longitude, fix.speed_kmh,
                fix.direction, fix.altitude, fix.device_time.isoformat(),
            )
            return False

        if msg_type == MSG_ERROR:
            error = str(msg.get("error") or "")
            _LOGGER.warning("bridge 错误回复: %s", error)
            self._fail_pending(BridgeError(error or "bridge 指令失败"))
            return False

        _LOGGER.debug("bridge 未知消息类型 %s: %s", msg_type, raw[:200])
        return False

    async def async_set_report_interval(self, device: str, interval: int) -> None:

        ws = self._ws
        if ws is None or ws.closed:
            raise BridgeError("bridge 未连接")

        key = f"set_report_interval:{device}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        stale = self._pending.pop(key, None)
        if stale is not None and not stale.done():
            stale.set_exception(BridgeError("请求已被新的设置覆盖"))
        self._pending[key] = fut
        try:
            await ws.send_json(
                {
                    "action": "set_report_interval",
                    "service_id": self.service_id,
                    "device": device,
                    "interval": interval,
                }
            )
            await asyncio.wait_for(fut, COMMAND_ACK_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise BridgeError("等待服务端回执超时") from err
        finally:
            self._pending.pop(key, None)

    async def async_refresh_location(self, device: str) -> None:

        ws = self._ws
        if ws is None or ws.closed:
            raise BridgeError("bridge 未连接")

        key = f"refresh_location:{device}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        stale = self._pending.pop(key, None)
        if stale is not None and not stale.done():
            stale.set_exception(BridgeError("请求已被新的刷新覆盖"))
        self._pending[key] = fut
        try:
            await ws.send_json(
                {
                    "action": "refresh_location",
                    "service_id": self.service_id,
                    "device": device,
                }
            )
            await asyncio.wait_for(fut, COMMAND_ACK_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise BridgeError("等待服务端回执超时") from err
        finally:
            self._pending.pop(key, None)

    async def async_reboot_device(self, device: str) -> None:

        ws = self._ws
        if ws is None or ws.closed:
            raise BridgeError("bridge 未连接")

        key = f"reboot_device:{device}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        stale = self._pending.pop(key, None)
        if stale is not None and not stale.done():
            stale.set_exception(BridgeError("请求已被新的重启指令覆盖"))
        self._pending[key] = fut
        try:
            await ws.send_json(
                {
                    "action": "reboot_device",
                    "service_id": self.service_id,
                    "device": device,
                }
            )
            await asyncio.wait_for(fut, COMMAND_ACK_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise BridgeError("等待服务端回执超时") from err
        finally:
            self._pending.pop(key, None)

    async def async_refresh_devices(self) -> bool:

        ws = self._ws
        if ws is None or ws.closed:
            _LOGGER.debug("[bridge] 刷新设备列表跳过：连接未就绪")
            return False

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[REFRESH_KEY] = fut
        try:
            await ws.send_json(
                {"action": "refresh", "service_id": self.service_id}
            )
            await asyncio.wait_for(fut, COMMAND_ACK_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise BridgeError("等待设备列表刷新超时") from err
        finally:
            self._pending.pop(REFRESH_KEY, None)
        return True

    def _fail_pending(self, err: Exception) -> None:

        pending = list(self._pending.values())
        self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(err)

    def _on_state(self, device: str, state: bool) -> None:
        try:
            self.on_state(device, state)
        except Exception:
            _LOGGER.warning(
                "bridge on_state 回调异常 device=%s state=%s",
                device, state, exc_info=True,
            )

    @callback
    def _set_all_offline(self) -> None:

        if self.online:
            self.online = False
        devices = list(self.devices)
        if not devices:
            return
        _LOGGER.debug("[bridge] 连接离线，设备置离线 %d 台", len(devices))
        for device in devices:
            self._on_state(device, False)
