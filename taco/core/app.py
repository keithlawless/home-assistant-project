"""taco-core: shared state and API for the screen, the voice loop and Hermes.

Serves the kiosk UI, pushes live updates over /ws, keeps weather fresh, and
stores settings. Later phases add timers, the device registry and lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .assistant import Assistant
from .db import Database
from .hub import Hub
from .intents import normalize, parse_clock
from .weather import fetch_forecast, geocode, locate_by_ip

log = logging.getLogger("taco.core")

UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
WEATHER_REFRESH_S = 600
WEATHER_RETRY_S = 60


class State:
    def __init__(self):
        self.db = Database()
        self.hub = Hub()
        self.http = httpx.AsyncClient(timeout=10.0)
        self.weather: dict[str, Any] | None = None
        self.voice: dict[str, Any] = {"state": "idle"}
        self.refresh_now = asyncio.Event()
        self.assistant = Assistant(self)

    def timers_payload(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self.db.timers.all()]

    def snapshot(self) -> dict[str, Any]:
        return {"weather": self.weather, "settings": self.db.settings(), "voice": self.voice,
                "timers": self.timers_payload()}

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
    tasks = [asyncio.create_task(state.weather_loop()), asyncio.create_task(state.timer_loop())]
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


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return state.db.settings()


@app.put("/api/settings")
async def put_settings(changes: dict[str, Any]) -> dict[str, Any]:
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
