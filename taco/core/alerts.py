"""Severe weather alerts from the US National Weather Service (api.weather.gov).

Free and keyless; US locations only. Severe and extreme alerts (warnings)
are announced once by voice and shown on screen while active.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

ALERTS_URL = "https://api.weather.gov/alerts/active"
HEADERS = {"User-Agent": "Taco kitchen assistant (github.com/keithlawless/home-assistant-project)",
           "Accept": "application/geo+json"}
ANNOUNCE = {"Severe", "Extreme"}


async def fetch_alerts(http: httpx.AsyncClient, lat: float, lon: float) -> list[dict[str, Any]]:
    r = await http.get(ALERTS_URL, params={"point": f"{lat:.4f},{lon:.4f}"}, headers=HEADERS)
    if r.status_code == 404:   # outside NWS coverage
        return []
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
            continue
        out.append({
            "id": p.get("id") or f.get("id"),
            "event": p.get("event", "Weather alert"),
            "severity": p.get("severity", "Unknown"),
            "headline": p.get("headline") or p.get("event"),
            "ends": p.get("ends") or p.get("expires"),
            "announce": p.get("severity") in ANNOUNCE,
        })
    # Most severe first.
    order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    return sorted(out, key=lambda a: order.get(a["severity"], 4))
