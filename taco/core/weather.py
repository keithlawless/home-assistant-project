"""Weather from Open-Meteo (free, no API key) plus location lookup."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# WMO weather code -> (description, icon key). The UI draws the icons.
WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "clear"),
    1: ("Mostly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "sleet"),
    57: ("Freezing drizzle", "sleet"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "sleet"),
    67: ("Freezing rain", "sleet"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"),
    81: ("Rain showers", "rain"),
    82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorms", "thunder"),
    96: ("Thunderstorms with hail", "thunder"),
    99: ("Thunderstorms with hail", "thunder"),
}


def describe(code: int, is_day: bool = True) -> dict[str, str]:
    text, icon = WMO.get(code, ("Unknown", "cloudy"))
    if not is_day and icon in ("clear", "partly"):
        icon += "-night"
    return {"condition": text, "icon": icon}


async def locate_by_ip(http: httpx.AsyncClient) -> dict[str, Any] | None:
    """Approximate location from the public IP; used once on first start."""
    try:
        r = await http.get("http://ip-api.com/json/",
                           params={"fields": "status,city,regionName,lat,lon,timezone"})
        d = r.json()
        if d.get("status") == "success":
            return {"name": f"{d['city']}, {d['regionName']}", "lat": d["lat"],
                    "lon": d["lon"], "timezone": d["timezone"]}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("IP geolocation failed: %s", e)
    return None


async def geocode(http: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Search places by name or postal code, for the Settings screen."""
    name, _, region = query.partition(",")
    r = await http.get(GEOCODE_URL, params={"name": name.strip(), "count": 10, "language": "en"})
    r.raise_for_status()
    results = r.json().get("results", [])
    region = region.strip().lower()
    # Prefer matches in the named state/country, then towns over peaks and rivers.
    results.sort(key=lambda p: (
        bool(region) and region not in f"{p.get('admin1', '')} {p.get('country', '')}".lower(),
        not str(p.get("feature_code", "")).startswith("PPL"),
        -(p.get("population") or 0),
    ))
    out = []
    for p in results[:5]:
        region = p.get("admin1") or p.get("country", "")
        out.append({"name": f"{p['name']}, {region}".strip(", "), "lat": p["latitude"],
                    "lon": p["longitude"], "timezone": p.get("timezone", "auto")})
    return out


async def fetch_forecast(http: httpx.AsyncClient, location: dict[str, Any], units: str) -> dict[str, Any]:
    imperial = units == "fahrenheit"
    r = await http.get(FORECAST_URL, params={
        "latitude": location["lat"],
        "longitude": location["lon"],
        "timezone": location.get("timezone") or "auto",
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,is_day,wind_speed_10m",
        "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,sunrise,sunset",
        "forecast_days": 6,
    })
    r.raise_for_status()
    return _shape(r.json(), location, imperial)


def _shape(raw: dict[str, Any], location: dict[str, Any], imperial: bool) -> dict[str, Any]:
    cur, hourly, daily = raw["current"], raw["hourly"], raw["daily"]

    # Next 12 hours starting from the current hour.
    now_hour = cur["time"][:13]
    start = next((i for i, t in enumerate(hourly["time"]) if t[:13] >= now_hour), 0)
    hours = [
        {"time": hourly["time"][i],
         "temp": round(hourly["temperature_2m"][i]),
         "precip": hourly["precipitation_probability"][i],
         **describe(hourly["weather_code"][i], bool(hourly["is_day"][i]))}
        for i in range(start, min(start + 12, len(hourly["time"])))
    ]
    days = [
        {"date": daily["time"][i],
         "high": round(daily["temperature_2m_max"][i]),
         "low": round(daily["temperature_2m_min"][i]),
         "precip": daily["precipitation_probability_max"][i],
         **describe(daily["weather_code"][i])}
        for i in range(len(daily["time"]))
    ]
    return {
        "location": location["name"],
        "units": {"temp": "°F" if imperial else "°C", "wind": "mph" if imperial else "km/h"},
        "current": {
            "temp": round(cur["temperature_2m"]),
            "feels_like": round(cur["apparent_temperature"]),
            "humidity": cur["relative_humidity_2m"],
            "wind": round(cur["wind_speed_10m"]),
            **describe(cur["weather_code"], bool(cur["is_day"])),
        },
        "today": {**days[0], "sunrise": daily["sunrise"][0], "sunset": daily["sunset"][0]},
        "hourly": hours,
        "daily": days,
        "updated": time.time(),
    }
