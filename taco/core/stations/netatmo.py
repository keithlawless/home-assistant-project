"""Netatmo Weather Station over its cloud API (OAuth, scope read_station).

The base station is the indoor unit; NAModule1 is the outdoor module,
NAModule2 the wind gauge, NAModule3 the rain gauge and NAModule4 extra indoor
modules (ignored for now). The API always reports metric units.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..oauth import OAuthToken
from .base import Indoor, Outdoor, Reading

TOKEN_URL = "https://api.netatmo.com/oauth2/token"
DATA_URL = "https://api.netatmo.com/api/getstationsdata"


class NetatmoStation:
    name = "Netatmo"

    def __init__(self, cfg: dict[str, Any], store_dir: Path):
        self._device_id = cfg.get("device_id") or None
        self._auth = OAuthToken("netatmo", TOKEN_URL, cfg.get("client_id", ""), cfg.get("client_secret", ""),
                                cfg.get("refresh_token", ""), store_dir)

    async def read(self, http: httpx.AsyncClient) -> Reading:
        params = {"get_favorites": "false", **({"device_id": self._device_id} if self._device_id else {})}
        r = await self._auth.request(http, "GET", DATA_URL, params=params)
        r.raise_for_status()
        return parse(r.json())


def parse(body: dict[str, Any]) -> Reading:
    devices = body.get("body", {}).get("devices") or []
    if not devices:
        raise ValueError("Netatmo: no weather station on this account")
    base = devices[0]
    times: list[float] = []
    indoor, outdoor = Indoor(), Outdoor()

    if base.get("reachable", True) and (d := base.get("dashboard_data")):
        indoor = Indoor(temp_c=d.get("Temperature"), humidity=d.get("Humidity"), co2_ppm=d.get("CO2"),
                        noise_db=d.get("Noise"), pressure_mbar=d.get("Pressure"))
        times.append(d.get("time_utc", 0))

    for m in base.get("modules", []):
        d = m.get("dashboard_data")
        if not m.get("reachable", True) or not d:
            continue
        kind = m.get("type")
        if kind == "NAModule1":
            outdoor.temp_c, outdoor.humidity = d.get("Temperature"), d.get("Humidity")
        elif kind == "NAModule2":
            outdoor.wind_kmh, outdoor.gust_kmh = d.get("WindStrength"), d.get("GustStrength")
            outdoor.wind_deg = d.get("WindAngle")
        elif kind == "NAModule3":
            outdoor.rain_1h_mm, outdoor.rain_24h_mm = d.get("sum_rain_1"), d.get("sum_rain_24")
        else:
            continue
        times.append(d.get("time_utc", 0))

    if not times:
        raise ValueError("Netatmo: station and modules are all unreachable")
    return Reading(measured_at=float(max(times)), outdoor=outdoor, indoor=indoor)
