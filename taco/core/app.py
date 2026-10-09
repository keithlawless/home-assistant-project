"""taco-core: shared state and API for the screen, the voice loop and Hermes.

Serves the kiosk UI, pushes live updates over /ws, keeps weather fresh, and
stores settings. Later phases add timers, the device registry and lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from taco.config import load_config
from taco.voices import VOICES, is_known

from .assistant import Assistant
from .db import Database
from .devices.base import DeviceError
from .devices.kasa import KasaDriver
from .devices.manager import DeviceManager, Registry
from .devices.matter import MatterDriver
from .hub import Hub
from .intents import normalize, parse_clock
from .weather import fetch_forecast, geocode, locate_by_ip

log = logging.getLogger("taco.core")

UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
WEATHER_REFRESH_S = 600
WEATHER_RETRY_S = 60


class State:
    def __init__(self):
        self.cfg = load_config()
        self.db = Database()
        self.hub = Hub()
        self.http = httpx.AsyncClient(timeout=10.0)
        self.weather: dict[str, Any] | None = None
        self.voice: dict[str, Any] = {"state": "idle"}
        self.refresh_now = asyncio.Event()
        kasa = self.cfg["kasa"]
        self.matter = MatterDriver(self.cfg["matter"]["url"], on_change=self.on_devices_changed)
        self.devices = DeviceManager(
            Registry(self.db.conn, self.db.lock),
            {"kasa": KasaDriver(kasa["username"], kasa["password"]), "matter": self.matter},
            rooms=lambda: self.db.settings()["rooms"],
            on_change=self.on_devices_changed,
        )
        self._last_devices = ""
        self.assistant = Assistant(self)

    async def on_devices_changed(self) -> None:
        """Push device state to screens, but only when something changed."""
        payload = self.devices.payload()
        encoded = json.dumps(payload, sort_keys=True)
        if encoded != self._last_devices:
            self._last_devices = encoded
            await self.hub.broadcast({"type": "devices", "devices": payload})

    def timers_payload(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self.db.timers.all()]

    def snapshot(self) -> dict[str, Any]:
        return {"weather": self.weather, "settings": self.db.settings(), "voice": self.voice,
                "timers": self.timers_payload(), "devices": self.devices.payload(),
                "found": self.devices.found_payload()}

    async def broadcast_timers(self) -> None:
        await self.hub.broadcast({"type": "timers", "timers": self.timers_payload()})

    async def timer_loop(self) -> None:
        while True:
            if self.db.timers.tick():
                await self.broadcast_timers()
            await asyncio.sleep(0.25)

    async def ensure_location(self) -> None:
        if self.db.settings()["location"] is None:
            if loc := await locate_by_ip(self.http):
                self.db.update_settings({"location": loc})
                log.info("location set from IP: %s", loc["name"])

    async def weather_loop(self) -> None:
        while True:
            delay = WEATHER_REFRESH_S
            try:
                await self.ensure_location()
                settings = self.db.settings()
                if settings["location"]:
                    self.weather = await fetch_forecast(self.http, settings["location"], settings["units"])
                    await self.hub.broadcast({"type": "weather", "weather": self.weather})
                else:
                    delay = WEATHER_RETRY_S
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("weather refresh failed: %s", e)
                delay = WEATHER_RETRY_S
            self.refresh_now.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.refresh_now.wait(), delay)


state: State


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = State()
    tasks = [asyncio.create_task(c) for c in (
        state.weather_loop(), state.timer_loop(), state.matter.run(), state.devices.poll_loop())]
    yield
    for task in tasks:
        task.cancel()
    await state.http.aclose()


app = FastAPI(title="taco-core", lifespan=lifespan)


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return state.snapshot()


@app.get("/api/weather")
async def get_weather() -> dict[str, Any]:
    if state.weather is None:
        raise HTTPException(503, "weather not loaded yet")
    return state.weather


@app.get("/api/weather/place")
async def weather_for_place(q: str) -> dict[str, Any]:
    """Forecast for somewhere other than home ("Sunday River, Maine")."""
    try:
        places = await geocode(state.http, q)
        if not places:
            raise HTTPException(404, f"couldn't find a place called {q!r}")
        return await fetch_forecast(state.http, places[0], state.db.settings()["units"])
    except httpx.HTTPError as e:
        raise HTTPException(502, f"weather lookup failed: {e}") from None


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return state.db.settings()


@app.put("/api/settings")
async def put_settings(changes: dict[str, Any]) -> dict[str, Any]:
    if "voice" in changes and changes["voice"] is not None and not is_known(changes["voice"]):
        raise HTTPException(400, f"unknown voice {changes['voice']!r}")
    if changes.get("wake_sensitivity", "medium") not in ("low", "medium", "high"):
        raise HTTPException(400, "wake_sensitivity must be low, medium or high")
    try:
        settings = state.db.update_settings(changes)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None
    await state.hub.broadcast({"type": "settings", "settings": settings})
    if {"location", "units"} & changes.keys():
        state.refresh_now.set()
    return settings


@app.get("/api/geocode")
async def search_places(q: str) -> list[dict[str, Any]]:
    try:
        return await geocode(state.http, q)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"place search failed: {e}") from None


@app.post("/api/intent")
async def intent(body: dict[str, Any]) -> dict[str, Any]:
    """Fast path for taco-voice: handle common requests without the LLM."""
    return await state.assistant.handle(body.get("text", ""))


# --- timers, alarms, reminders (used by the UI and by taco-mcp for Hermes) ---

def _timer_or_404(timer_id: int):
    if (t := state.db.timers.get(timer_id)) is None:
        raise HTTPException(404, f"no timer {timer_id}")
    return t


@app.get("/api/timers")
async def list_timers() -> list[dict[str, Any]]:
    return state.timers_payload()


@app.post("/api/timers")
async def create_timer(body: dict[str, Any]) -> dict[str, Any]:
    try:
        t = state.db.timers.add_timer(float(body["seconds"]), body.get("label"))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"bad timer: {e}") from None
    await state.broadcast_timers()
    return t.to_dict()


@app.post("/api/alarms")
async def create_alarm(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"at": epoch seconds} or {"time": "7:30 am"} plus optional label."""
    if "at" in body:
        at = float(body["at"])
    elif (when := parse_clock(normalize(str(body.get("time", ""))), datetime.now())) is not None:
        at = when.timestamp()
    else:
        raise HTTPException(400, "give 'at' (epoch seconds) or a 'time' like '7:30 am'")
    t = state.db.timers.add_alarm(at, body.get("label"))
    await state.broadcast_timers()
    return t.to_dict()


@app.post("/api/reminders")
async def create_reminder(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"message", and "in_seconds" or "at" (epoch seconds)}."""
    if not body.get("message"):
        raise HTTPException(400, "reminder needs a message")
    at = float(body["at"]) if "at" in body else time.time() + float(body.get("in_seconds", 0))
    t = state.db.timers.add_reminder(at, body["message"])
    await state.broadcast_timers()
    return t.to_dict()


@app.post("/api/timers/{timer_id}/add")
async def add_time(timer_id: int, body: dict[str, Any]) -> dict[str, Any]:
    t = state.db.timers.add_time(_timer_or_404(timer_id), float(body.get("seconds", 60)))
    await state.broadcast_timers()
    return t.to_dict()


@app.post("/api/timers/{timer_id}/{action}")
async def timer_action(timer_id: int, action: str) -> dict[str, Any]:
    t = _timer_or_404(timer_id)
    if action == "pause":
        t = state.db.timers.pause(t)
    elif action == "resume":
        t = state.db.timers.resume(t)
    else:
        raise HTTPException(404, f"unknown action {action}")
    await state.broadcast_timers()
    return t.to_dict()


@app.delete("/api/timers/{timer_id}")
async def delete_timer(timer_id: int) -> dict[str, bool]:
    state.db.timers.remove([_timer_or_404(timer_id).id])
    await state.broadcast_timers()
    return {"ok": True}


@app.post("/api/stop")
async def stop(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stop button / touch: silence ringing timers and cut off Taco's speech.
    taco-voice sends {"speech": false} when the wake word interrupts an alarm,
    so the conversation that follows isn't cut off too."""
    stopped = state.db.timers.stop_ringing()
    if (body or {}).get("speech", True):
        await state.hub.broadcast({"type": "voice_command", "command": "stop"})
    await state.broadcast_timers()
    return {"stopped": [t.id for t in stopped]}


# --- devices -------------------------------------------------------------------

def _device_or_404(device_id: int) -> dict[str, Any]:
    if (d := state.devices.registry.get(device_id)) is None:
        raise HTTPException(404, f"no device {device_id}")
    return d


@app.get("/api/devices")
async def list_devices() -> list[dict[str, Any]]:
    return state.devices.payload()


@app.post("/api/devices/scan")
async def scan_devices() -> list[dict[str, Any]]:
    """Look on the network; returns devices not yet added."""
    found = await state.devices.scan()
    await state.hub.broadcast({"type": "found", "found": found})
    return found


@app.get("/api/devices/found")
async def found_devices() -> list[dict[str, Any]]:
    return state.devices.found_payload()


@app.post("/api/devices")
async def add_device(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"key" from scan results, optional "name", optional "room"}."""
    room = body.get("room")
    try:
        device = state.devices.add(body["key"], body.get("name"), room)
    except KeyError as e:
        raise HTTPException(400, str(e)) from None
    if room:
        _remember_room(room)
    await state.devices.refresh()
    await state.hub.broadcast({"type": "found", "found": state.devices.found_payload()})
    return device


def _remember_room(room: str) -> None:
    rooms = state.db.settings()["rooms"]
    if room.lower() not in (r.lower() for r in rooms):
        state.db.update_settings({"rooms": [*rooms, room]})


@app.patch("/api/devices/{device_id}")
async def update_device(device_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Rename, move to a room, or set nicknames (aliases)."""
    _device_or_404(device_id)
    changes = {k: v for k, v in body.items() if k in ("name", "room", "aliases")}
    if not changes:
        raise HTTPException(400, "nothing to change (name, room, aliases)")
    if changes.get("room"):
        _remember_room(changes["room"])
    device = state.devices.registry.update(device_id, **changes)
    await state.on_devices_changed()
    return device  # type: ignore[return-value]


@app.delete("/api/devices/{device_id}")
async def remove_device(device_id: int) -> dict[str, bool]:
    d = _device_or_404(device_id)
    state.devices.registry.remove(device_id)
    state.devices.states.pop(device_id, None)
    if d["protocol"] == "matter" and not _node_still_used(d):
        with contextlib.suppress(DeviceError):
            await state.matter.remove(d["address"])
    await state.on_devices_changed()
    return {"ok": True}


def _node_still_used(device: dict[str, Any]) -> bool:
    """Another endpoint of this Matter node is still registered, so keep the node."""
    node = device["address"].split("-")[0]
    return any(o["protocol"] == "matter" and o["address"].split("-")[0] == node
                   for o in state.devices.registry.all())


@app.post("/api/devices/control")
async def control_devices(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"ids": [...]} or {"target": "living room"}, plus "on" (bool) or "level" (0-100)."""
    if "ids" in body:
        ids = [int(i) for i in body["ids"]]
    else:
        m = state.devices.resolve(str(body.get("target", "")))
        if m is None:
            raise HTTPException(404, f"no device or room matches {body.get('target')!r}")
        if m.ambiguous:
            names = [d["name"] for d in state.devices.registry.all() if d["id"] in m.ids]
            raise HTTPException(409, f"ambiguous; matches {', '.join(names)}")
        ids = m.ids
    if "level" in body:
        ok, failed = await state.devices.control(ids, "level", int(body["level"]))
    elif "on" in body:
        ok, failed = await state.devices.control(ids, "power", bool(body["on"]))
    else:
        raise HTTPException(400, "give 'on' or 'level'")
    return {"ok": [d["name"] for d in ok], "not_responding": [d["name"] for d in failed]}


@app.post("/api/matter/commission")
async def commission_matter(body: dict[str, Any]) -> list[dict[str, Any]]:
    """body: {"code": setup or pairing code, "network_only": bool}.
    network_only=true for a device already on Wi-Fi and shared from another app;
    false for a brand-new device in pairing mode (Bluetooth + Wi-Fi credentials
    from [matter] in /srv/taco/config.toml)."""
    m = state.cfg["matter"]
    try:
        found = await state.matter.commission(str(body["code"]), bool(body.get("network_only", True)),
                                              m.get("wifi_ssid"), m.get("wifi_password"))
    except (KeyError, DeviceError) as e:
        raise HTTPException(400, f"commissioning failed: {e}") from None
    state.devices.add_found(found)
    out = state.devices.found_payload()
    await state.hub.broadcast({"type": "found", "found": out})
    return out


@app.get("/api/voices")
async def list_voices() -> list[dict[str, str]]:
    return VOICES


@app.post("/api/voice/preview")
async def preview_voice(body: dict[str, Any]) -> dict[str, bool]:
    """Have taco-voice say a sample sentence in a voice without switching to it."""
    if not is_known(body.get("voice", "")):
        raise HTTPException(400, "unknown voice")
    await state.hub.broadcast({"type": "voice_command", "command": "preview", "voice": body["voice"]})
    return {"ok": True}


@app.post("/api/voice/event")
async def voice_event(event: dict[str, Any]) -> dict[str, bool]:
    """State updates from taco-voice: listening, heard text, tool use, reply."""
    if event.get("type") == "state":
        state.voice = {"state": event.get("state", "idle")}
    await state.hub.broadcast({"type": "voice", "event": event})
    return {"ok": True}


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await state.hub.connect(ws, state.snapshot())
    try:
        while True:
            await ws.receive_text()  # the UI only listens; this detects disconnects
    except WebSocketDisconnect:
        state.hub.disconnect(ws)


# The built UI is served last so /api and /ws take precedence.
if UI_DIST.exists():
    app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8700, log_level="warning")


if __name__ == "__main__":
    main()
