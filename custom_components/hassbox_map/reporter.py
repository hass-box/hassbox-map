from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .data_client import HassBoxDataClient

_LOGGER = logging.getLogger(__name__)

REPORT_INTERVAL = 30

class LocationReporter:

    def __init__(self, hass: HomeAssistant, client: HassBoxDataClient, enabled: bool) -> None:
        self._hass = hass
        self._client = client
        self._enabled = enabled

        self._pending: dict[str, Any] = {}

        self._last_report: dict[str, float] = {}

        self._timers: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:

        self._enabled = value
        if not value:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._pending.clear()

    def report(self, sn: str, fix) -> None:

        if not self._enabled:
            return

        self._pending[sn] = fix

        elapsed = self._hass.loop.time() - self._last_report.get(sn, 0.0)
        if elapsed >= REPORT_INTERVAL:

            self._flush(sn)
            return

        if sn in self._timers:
            return
        self._timers[sn] = self._hass.loop.call_later(
            REPORT_INTERVAL - elapsed, self._on_timer, sn
        )

    def _on_timer(self, sn: str) -> None:

        self._timers.pop(sn, None)
        self._flush(sn)

    def _flush(self, sn: str) -> None:

        fix = self._pending.pop(sn, None)
        if fix is None:
            return

        self._last_report[sn] = self._hass.loop.time()
        self._hass.async_create_task(self._do_report(sn, fix))

    async def _do_report(self, sn: str, fix) -> None:

        try:
            await self._client.report_location(sn, fix)
            _LOGGER.debug(
                "定位已上报 server sn=%s lat=%.6f lng=%.6f",
                sn, fix.latitude, fix.longitude,
            )
        except Exception:
            _LOGGER.warning(
                "定位上报 server 失败 sn=%s", sn, exc_info=True,
            )
