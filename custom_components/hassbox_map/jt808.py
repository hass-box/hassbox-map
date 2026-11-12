from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

MSG_LOCATION_REPORT = 0x0200
MSG_LOCATION_QUERY_RESP = 0x0201

CST = timezone(timedelta(hours=8))

_LOCATION_BODY_LEN = 28

DROP_HEX_INVALID = "hex 非法"
DROP_TOO_SHORT = "帧太短"
DROP_NON_LOCATION = "非定位消息"
DROP_BODY_MISMATCH = "body_len 不匹配"
DROP_NO_FIX = "未定位 (GPS 无信号)"
DROP_BAD_TIME = "时间字段非法"

@dataclass
class LocationFix:

    latitude: float
    longitude: float
    altitude: int | None
    speed_kmh: float
    direction: int
    alarm: int
    status: int
    device_time: datetime
    msg_id: int
    gps_fix: bool
    moving: bool
    extras: dict[str, Any] = field(default_factory=dict)

def _be16(b: bytes, off: int) -> int:
    return (b[off] << 8) | b[off + 1]

def _be24(b: bytes, off: int) -> int:
    return (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]

def _be32(b: bytes, off: int) -> int:
    return (b[off] << 24) | (b[off + 1] << 16) | (b[off + 2] << 8) | b[off + 3]

_ALTITUDE_MIN = -500
_ALTITUDE_MAX = 9000

def _parse_altitude(raw: int) -> int | None:

    value = raw - 0x10000 if raw >= 0x8000 else raw
    return value if _ALTITUDE_MIN <= value <= _ALTITUDE_MAX else None

def _bcd_time(b: bytes) -> datetime | None:

    dec = lambda x: (x >> 4) * 10 + (x & 0x0F)
    try:
        return datetime(
            2000 + dec(b[0]),
            dec(b[1]),
            dec(b[2]),
            dec(b[3]),
            dec(b[4]),
            dec(b[5]),
            tzinfo=CST,
        )
    except ValueError:
        return None

def parse_frame(hex_str: str) -> tuple[LocationFix | None, str | None]:

    try:
        frame = bytes.fromhex(hex_str.strip())
    except ValueError:
        return None, DROP_HEX_INVALID

    if len(frame) < 13:
        return None, DROP_TOO_SHORT

    msg_id = _be16(frame, 0)
    body_attr = _be16(frame, 2)
    off = 4
    if body_attr & 0x4000:
        off += 11
    else:
        off += 6
    off += 2
    if body_attr & 0x2000:
        off += 4
    body_len = body_attr & 0x03FF
    body = frame[off : off + body_len]
    if len(body) < body_len:
        return None, DROP_BODY_MISMATCH

    if msg_id in (MSG_LOCATION_REPORT, MSG_LOCATION_QUERY_RESP):
        return _parse_location(msg_id, body)
    return None, DROP_NON_LOCATION

def _parse_location(
    msg_id: int, body: bytes
) -> tuple[LocationFix | None, str | None]:

    if msg_id == MSG_LOCATION_QUERY_RESP:
        body = body[2:]
    return _fix_from(msg_id, body)

def _fix_from(
    msg_id: int, body: bytes
) -> tuple[LocationFix | None, str | None]:

    if len(body) < _LOCATION_BODY_LEN:
        return None, DROP_TOO_SHORT
    basic = body[:_LOCATION_BODY_LEN]
    time_b = basic[22:28]
    status = _be32(basic, 4)

    gps_fix = bool(status & 0x02)

    lat_raw = _be32(basic, 8)
    lng_raw = _be32(basic, 12)

    if gps_fix:
        if status & 0x04:
            lat_raw = -lat_raw
        if status & 0x08:
            lng_raw = -lng_raw

    if not gps_fix and lat_raw == 0 and lng_raw == 0:
        device_time = _bcd_time(time_b)
        if device_time is None:
            return None, DROP_BAD_TIME
        return LocationFix(
            latitude=0.0,
            longitude=0.0,
            altitude=_parse_altitude(_be16(basic, 16)),
            speed_kmh=_be16(basic, 18) / 10,
            direction=_be16(basic, 20),
            alarm=_be32(basic, 0),
            status=status,
            device_time=device_time,
            msg_id=msg_id,
            gps_fix=False,
            moving=bool(status & 0x400000),
            extras=_parse_extras(body[_LOCATION_BODY_LEN:]),
        ), DROP_NO_FIX

    device_time = _bcd_time(time_b)
    if device_time is None:
        return None, DROP_BAD_TIME

    return LocationFix(
        latitude=lat_raw / 1e6,
        longitude=lng_raw / 1e6,
        altitude=_parse_altitude(_be16(basic, 16)),
        speed_kmh=_be16(basic, 18) / 10,
        direction=_be16(basic, 20),
        alarm=_be32(basic, 0),
        status=status,
        device_time=device_time,
        msg_id=msg_id,
        gps_fix=gps_fix,
        moving=bool(status & 0x400000),
        extras=_parse_extras(body[_LOCATION_BODY_LEN:]),
    ), None

def _text(b: bytes) -> str:

    return b.decode("utf-8", errors="ignore").strip("\x00 ")

def _parse_extras(data: bytes) -> dict[str, Any]:

    extras: dict[str, Any] = {}
    unknown: dict[str, str] = {}
    off = 0
    while off + 2 <= len(data):
        item_id = data[off]
        item_len = data[off + 1]
        off += 2
        if off + item_len > len(data):
            unknown[f"0x{item_id:02X}"] = data[off:]
            break
        value = data[off : off + item_len]
        off += item_len
        if not _parse_extra_item(item_id, value, extras):
            unknown[f"0x{item_id:02X}"] = value.hex()
    if unknown:
        extras["unknown"] = unknown
    return extras

def _parse_extra_item(item_id: int, v: bytes, out: dict[str, Any]) -> bool:

    n = len(v)
    if item_id == 0x01 and n >= 4:
        out["mileage_km"] = round(_be32(v, 0) / 10, 1)
    elif item_id == 0x02 and n >= 2:
        out["fuel_l"] = round(_be16(v, 0) / 10, 1)
    elif item_id == 0x30 and n >= 1:
        out["signal_percent"] = v[0]
    elif item_id == 0x31 and n >= 1:
        out["satellites"] = v[0]
    elif item_id == 0xE1 and n >= 4:
        cells: list[dict[str, int]] = []
        p = 4
        while p + 8 <= n:
            cells.append(
                {"lac": _be24(v, p), "cell_id": _be32(v, p + 3), "signal": v[p + 7]}
            )
            p += 8
        out["base_stations"] = {
            "mcc": _be16(v, 0),
            "mnc": _be16(v, 2),
            "cells": cells,
        }
    elif item_id == 0xE4 and n >= 2:
        out["power_source"] = "charging" if v[0] == 0 else "battery"
        out["battery_percent"] = v[1]
    elif item_id == 0xE5 and n >= 1:
        out["motion"] = "moving" if v[0] else "static"
    elif item_id == 0xE6 and n >= 1:
        out["location_sms"] = bool(v[0])
    elif item_id == 0xE7 and n >= 8:
        alarm = _be16(v, 0)
        switch = _be16(v, 2)
        out["status_ext"] = {
            "alarm": alarm,
            "vibration_alarm": bool(alarm & 0x0001),
            "switch": switch,
            "armed": bool(switch & 0x0001),
            "sleep": bool(switch & 0x0002),
            "sleep_by_e7": bool(switch & 0x0004),
            "reserved": v[4:8].hex(),
        }
    elif item_id == 0xEB and n >= 3:
        cells = []
        p = 3
        while p + 5 <= n:
            cells.append(
                {"lac": _be16(v, p), "cell_id": _be16(v, p + 2), "signal": v[p + 4]}
            )
            p += 5
        out["base_stations_2g"] = {
            "mcc": _be16(v, 0),
            "mnc": v[2],
            "cells": cells,
        }
    elif item_id == 0xEC and n >= 7:
        aps = []
        p = 0
        while p + 7 <= n:
            aps.append(
                {
                    "mac": ":".join(f"{b:02x}" for b in v[p : p + 6]),
                    "signal": v[p + 6],
                }
            )
            p += 7
        out["wifi"] = aps
    elif item_id == 0xED and n >= 14:
        out["cdma"] = {
            "sid": _be16(v, 0),
            "nid": v[2],
            "bid": _be16(v, 3),
            "lat": _be32(v, 5),
            "lng": _be32(v, 9),
            "signal": v[13],
        }
    elif item_id == 0xEE and n >= 10:
        out["base_stations_4g"] = {
            "mcc": _be16(v, 0),
            "mnc": v[2],
            "lac": _be16(v, 3),
            "cell_id": _be32(v, 5),
            "signal": v[9],
        }
    elif item_id == 0xF0 and n >= 2:
        out["external_voltage_v"] = round(_be16(v, 0) / 100, 2)
    elif item_id == 0xF1 and n >= 2:
        raw = _be16(v, 0)
        out["current_ma"] = raw - 0x10000 if raw >= 0x8000 else raw
    elif item_id == 0xF2 and n >= 1:
        out["gps_satellites"] = v[0]
    elif item_id == 0xF3 and n >= 1:
        out["beidou_satellites"] = v[0]
    elif item_id == 0xF4 and n >= 1:
        out["glonass_satellites"] = v[0]
    elif item_id == 0xF5 and n >= 1:
        out["signal_type"] = "4G" if v[0] == 1 else "2G"
    elif item_id == 0xF6 and n >= 8:
        out["imei"] = v[:8].hex().lstrip("0") or "0"
    elif item_id == 0xF7 and n >= 8:
        p = 6
        fields: list[str] = []
        for _ in range(3):
            if p >= n:
                break
            size = v[p]
            p += 1
            fields.append(_text(v[p : p + size]))
            p += size
        fields += [""] * (3 - len(fields))
        out["apn"] = {
            "mcc": v[0:3].hex(),
            "mnc": v[3:6].hex(),
            "name": fields[0],
            "username": fields[1],
            "password": fields[2],
        }
    elif item_id == 0x51 and n >= 8:
        temps = []
        for i in range(4):
            raw = _be16(v, i * 2)
            temps.append(
                round(-(raw - 0x8000) / 10, 1) if raw >= 0x8000 else round(raw / 10, 1)
            )
        out["temperatures_c"] = temps
    elif item_id == 0xF8 and n >= 2:
        out["steps"] = _be16(v, 0)
    elif item_id == 0xF9 and n >= 1:
        out["fake_point"] = bool(v[0])
    else:
        return False
    return True
