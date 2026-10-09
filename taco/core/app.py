"""taco-core: shared state and API for the screen, the voice loop and Hermes.

Serves the kiosk UI, pushes live updates over /ws, keeps weather fresh, and
stores settings. Later phases add timers, the device registry and lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .db import Database
from .hub import Hub
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

    def snapshot(self) -> dict[str, Any]:
        return {"weather": self.weather, "settings": self.db.settings(), "voice": self.voice}

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
    task = asyncio.create_task(state.weather_loop())
    yield
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
