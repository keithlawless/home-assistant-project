"""What a local weather station gives Taco, whatever its make.

A station reports any subset of these readings, always in metric. Taco uses
each one it gets for the current conditions and falls back to the forecast
service for the rest (taco/core/weather.py, apply_station).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class Outdoor:
    temp_c: float | None = None
    humidity: float | None = None       # percent
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    wind_deg: float | None = None
    rain_1h_mm: float | None = None
    rain_24h_mm: float | None = None


@dataclass
class Indoor:
    temp_c: float | None = None
    humidity: float | None = None
    co2_ppm: float | None = None
    noise_db: float | None = None
    pressure_mbar: float | None = None


@dataclass
class Reading:
    measured_at: float                  # epoch seconds of the newest measurement
    outdoor: Outdoor = field(default_factory=Outdoor)
    indoor: Indoor = field(default_factory=Indoor)


class Station(Protocol):
    name: str

    async def read(self, http: httpx.AsyncClient) -> Reading:
        """Latest readings. Raise httpx.HTTPError or ValueError when unavailable."""
        ...
