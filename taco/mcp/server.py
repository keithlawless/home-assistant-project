"""taco-mcp: Taco's household tools for Hermes, over MCP (stdio).

A thin layer over taco-core's REST API, so Hermes, the voice fast path and
the touchscreen all act on the same timers (and, from Phase 4, devices).
Hermes launches this as a subprocess; see hermes/configure.sh.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

CORE = os.environ.get("TACO_CORE_URL", "http://127.0.0.1:8700")

mcp = MCPServer(
    "taco",
    instructions="Tools for the household's kitchen assistant: timers, alarms, reminders, "
                 "local weather, and the home's smart plugs and lights.",
)
http = httpx.Client(base_url=CORE, timeout=10.0)


def _call(method: str, path: str, **kwargs: Any) -> Any:
    r = http.request(method, path, **kwargs)
    if r.status_code >= 400:
        raise ValueError(r.json().get("detail", r.text))
    return r.json()


def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%-I:%M %p on %A")


def _summary(t: dict[str, Any]) -> dict[str, Any]:
    out = {"id": t["id"], "kind": t["kind"], "state": t["state"]}
    if t["label"]:
        out["label"] = t["label"]
    if t["message"]:
        out["message"] = t["message"]
    if t["kind"] == "timer":
        out["remaining_seconds"] = round(t["remaining"])
    else:
        out["goes_off_at"] = _when(t["ends_at"])
    return out


def _find(label: str | None, timer_id: int | None, kind: str = "timer") -> list[dict[str, Any]]:
    timers = [t for t in _call("GET", "/api/timers") if t["kind"] == kind]
    if timer_id is not None:
        return [t for t in timers if t["id"] == timer_id]
    if label:
        want = label.lower()
        return [t for t in timers if t["label"] and (want in t["label"].lower() or t["label"].lower() in want)]
    return timers


# --- timers ------------------------------------------------------------------

@mcp.tool()
def set_timer(hours: float = 0, minutes: float = 0, seconds: float = 0, label: str | None = None) -> dict:
    """Start a kitchen countdown timer. Give it a short label when the user names
    what it's for ("pasta", "eggs"). Call once per timer for several timers."""
    total = hours * 3600 + minutes * 60 + seconds
    return _summary(_call("POST", "/api/timers", json={"seconds": total, "label": label}))


@mcp.tool()
def list_timers() -> list[dict]:
    """List running timers, alarms and reminders with time remaining."""
    return [_summary(t) for t in _call("GET", "/api/timers")]


@mcp.tool()
def cancel_timer(label: str | None = None, timer_id: int | None = None, cancel_all: bool = False,
                 kind: str = "timer") -> str:
    """Cancel timers (kind="timer"), alarms (kind="alarm") or reminders (kind="reminder")
    by label or id, or all of that kind with cancel_all=true."""
    matches = _find(label, timer_id, kind)
    if not matches:
        return f"No matching {kind} found."
    if len(matches) > 1 and not cancel_all:
        names = ", ".join(t["label"] or f"id {t['id']}" for t in matches)
        return f"Several {kind}s match ({names}); ask which one, or pass cancel_all."
    for t in matches:
        _call("DELETE", f"/api/timers/{t['id']}")
    return f"Canceled {len(matches)} {kind}{'s' if len(matches) != 1 else ''}."


@mcp.tool()
def add_time_to_timer(minutes: float = 0, seconds: float = 0, label: str | None = None,
                      timer_id: int | None = None) -> dict | str:
    """Add (or with negative values, remove) time from a running timer."""
    matches = _find(label, timer_id)
    if len(matches) != 1:
        return "No timer found." if not matches else "Several timers match; ask which one."
    return _summary(_call("POST", f"/api/timers/{matches[0]['id']}/add",
                          json={"seconds": minutes * 60 + seconds}))


# --- alarms and reminders ------------------------------------------------------

@mcp.tool()
def set_alarm(time: str, label: str | None = None) -> dict:
    """Set an alarm for a clock time like "7:30 am", "19:15" or "6 pm tomorrow"
    (the next time it occurs)."""
    return _summary(_call("POST", "/api/alarms", json={"time": time, "label": label}))


@mcp.tool()
def set_reminder(message: str, in_minutes: float | None = None, at_epoch_seconds: float | None = None) -> dict:
    """Have Taco speak a reminder later. Give either in_minutes, or an absolute
    time as Unix epoch seconds (local time zone is the household's). The message
    should read naturally after "Reminder:", e.g. "take the bread out"."""
    body: dict[str, Any] = {"message": message}
    if at_epoch_seconds is not None:
        body["at"] = at_epoch_seconds
    else:
        body["in_seconds"] = (in_minutes or 0) * 60
    return _summary(_call("POST", "/api/reminders", json=body))


# --- weather -------------------------------------------------------------------

@mcp.tool()
def get_weather() -> dict:
    """Current conditions and today's forecast at the house. Prefer this over a
    web search for local weather."""
    w = _call("GET", "/api/weather")
    return {"location": w["location"], "units": w["units"], "current": w["current"], "today": w["today"],
            "next_hours": w["hourly"][:12]}


@mcp.tool()
def get_forecast() -> dict:
    """Daily forecast for the next several days at the house (highs, lows, rain chance)."""
    w = _call("GET", "/api/weather")
    return {"location": w["location"], "units": w["units"], "days": w["daily"]}


# --- devices -------------------------------------------------------------------

def _device(d: dict[str, Any]) -> dict[str, Any]:
    st = d["state"]
    out = {"id": d["id"], "name": d["name"], "room": d["room"], "kind": d["kind"],
           "online": st["online"], "on": st["on"]}
    if "level" in d["capabilities"]:
        out["level"] = st["level"]
    if d["aliases"]:
        out["nicknames"] = d["aliases"]
    return out


@mcp.tool()
def list_devices() -> list[dict]:
    """All registered smart plugs, lights and switches with their room and current state."""
    return [_device(d) for d in _call("GET", "/api/devices")]


@mcp.tool()
def control_devices(on: bool | None = None, level: int | None = None, target: str | None = None,
                    device_ids: list[int] | None = None) -> dict:
    """Turn devices on/off (on=true/false) or set brightness (level 0-100).
    Pick devices by device_ids (from list_devices) or by a spoken target such as
    a device name, a room ("living room") or "everything"."""
    body: dict[str, Any] = {"ids": device_ids} if device_ids else {"target": target or ""}
    if level is not None:
        body["level"] = level
    elif on is not None:
        body["on"] = on
    else:
        raise ValueError("give on or level")
    return _call("POST", "/api/devices/control", json=body)


@mcp.tool()
def find_new_devices() -> list[dict]:
    """Scan the network for Kasa plugs and commissioned Matter devices that aren't
    added yet. Then tell the user what was found and ask whether to keep each
    device's own name and suggested room, before calling add_device."""
    return [{"key": f["key"], "name": f["name"], "kind": f["kind"], "model": f["model"],
             "suggested_room": f["suggested_room"]} for f in _call("POST", "/api/devices/scan")]


@mcp.tool()
def add_device(key: str, name: str | None = None, room: str | None = None) -> dict:
    """Add a device from find_new_devices. Name and room default to the device's
    own name and the room guessed from it."""
    d = _call("POST", "/api/devices", json={"key": key, "name": name, "room": room})
    return {"id": d["id"], "name": d["name"], "room": d["room"]}


@mcp.tool()
def update_device(device_id: int, name: str | None = None, room: str | None = None,
                  nicknames: list[str] | None = None) -> dict:
    """Rename a device, move it to a room, or set nicknames it should answer to
    (e.g. ["reading lamp"]). Nicknames replace the existing list."""
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if room:
        body["room"] = room
    if nicknames is not None:
        body["aliases"] = nicknames
    d = _call("PATCH", f"/api/devices/{device_id}", json=body)
    return {"id": d["id"], "name": d["name"], "room": d["room"], "nicknames": d["aliases"]}


@mcp.tool()
def remove_device(device_id: int) -> str:
    """Remove a device from Taco. Confirm with the user first."""
    _call("DELETE", f"/api/devices/{device_id}")
    return "Removed."


@mcp.tool()
def list_rooms() -> list[str]:
    """Rooms devices can be assigned to. A new room is created by assigning a device to it."""
    return _call("GET", "/api/settings")["rooms"]


@mcp.tool()
def add_matter_device(code: str, already_on_wifi: bool = True) -> list[dict]:
    """Commission a Matter-over-Wi-Fi device with its 11-digit setup code or QR
    payload ("MT:..."). Use already_on_wifi=true for a device already set up in
    another app (Apple Home, Google Home, Alexa) that gave a pairing code; false
    for a brand-new device in pairing mode (set up over Bluetooth, takes up to a
    couple of minutes). Afterwards the device appears in find_new_devices."""
    found = _call("POST", "/api/matter/commission", json={"code": code, "network_only": already_on_wifi},
                  timeout=330)
    return [{"key": f["key"], "name": f["name"], "kind": f["kind"]} for f in found]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
