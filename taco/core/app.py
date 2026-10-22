"""taco-core: shared state and API for the screen, the voice loop and Hermes.

Serves the kiosk UI, pushes live updates over /ws, keeps weather fresh, and
stores settings. Later phases add timers, the device registry and lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from taco.config import TACO_HOME, load_config
from taco.voices import VOICES, is_known

from .alerts import fetch_alerts
from .assistant import Assistant
from .calendar import fetch_events
from .db import Database
from .devices.base import DeviceError
from .devices.kasa import KasaDriver
from .devices.manager import DeviceManager, Registry
from .devices.matter import MatterDriver
from .health import HealthMonitor, Limits, card, evaluate
from .hub import Hub
from .intents import normalize, parse_clock
from .library import Library, Plan
from .lists import ListStore
from .media import MUSIC_SINK, RadioPlayer, SpotifyState, search_stations
from .notifications import NotificationStore, in_window
from .oauth import OAuthError
from .scenes import SceneStore, scene_key
from .scheduler import Scheduler, ScheduleStore, describe
from .speakers import KITCHEN, Speakers, resolve
from .stations import Reading, make_station
from .weather import apply_station, fetch_forecast, geocode, locate_by_ip
from .webhook import MAX_BODY_BYTES, Rejected, WebhookGuard, forward, parse_payload

log = logging.getLogger("taco.core")

UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
WEATHER_REFRESH_S = 600
WEATHER_RETRY_S = 60
ALERTS_REFRESH_S = 300
CALENDAR_REFRESH_S = 900


class State:
    def __init__(self):
        self.cfg = load_config()
        self.db = Database()
        self.hub = Hub()
        self.http = httpx.AsyncClient(timeout=10.0)
        self.forecast: dict[str, Any] | None = None   # Open-Meteo, as fetched
        self.weather: dict[str, Any] | None = None    # forecast with station readings applied
        self.station = make_station(self.cfg["weather_station"], TACO_HOME / "data" / "oauth")
        self.station_reading: Reading | None = None
        self.station_error: str | None = None
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
        self.lists = ListStore(self.db.conn, self.db.lock)
        self.scenes = SceneStore(self.db.conn, self.db.lock)
        self.notifications = NotificationStore(self.db.conn, self.db.lock)
        self.webhook = WebhookGuard(self.cfg["webhooks"]["senders"])
        self._forwards: set[asyncio.Task] = set()   # keep in-flight forwards alive
        h = self.cfg["health"]
        self.health = HealthMonitor(Limits(h["cpu_percent"], h["memory_percent"], h["temp_c"], h["clear_margin"]),
                                    window_s=h["window_s"], interval_s=h["interval_s"])
        self.media = RadioPlayer(MUSIC_SINK, on_change=self.broadcast_media)
        self.spotify = SpotifyState()
        self.library = Library(self.cfg["music"])
        self.speakers = Speakers(self.cfg["speakers"]["idle_revert_min"] * 60,
                                 music_playing=lambda: self.music_playing, on_change=self.broadcast_media)
        self.recipe: dict[str, Any] | None = None
        self.alerts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []   # calendar, today + 6 days
        self._announced_alerts: set[str] = set()
        self.schedules = ScheduleStore(self.db.conn, self.db.lock)
        self.scheduler = Scheduler(
            self.schedules,
            run_actions=self.run_actions,
            sun=lambda: self.weather["today"] if self.weather else None,
            vacation=lambda: bool(self.db.settings()["vacation_mode"]),
            vacation_devices=lambda: [d["id"] for d in self.devices.registry.all()
                                      if d["kind"] in ("light", "plug", "dimmer")],
            set_power=self._vacation_power,
        )
        self.assistant = Assistant(self)

    def media_status(self) -> dict[str, Any] | None:
        status = self.media.status() or self.spotify.status()
        if status and self.speakers.current:
            status = {**status, "speaker": self.speakers.current.name}
        return status

    @property
    def music_playing(self) -> bool:
        return self.media.playing or self.spotify.playing

    async def stop_music(self) -> None:
        if self.media.playing:
            await self.media.stop()
        if self.spotify.playing:
            await self.spotify.stop()
            await self.broadcast_media()

    async def broadcast_media(self) -> None:
        await self.hub.broadcast({"type": "media", "media": self.media_status()})

    async def broadcast_recipe(self) -> None:
        await self.hub.broadcast({"type": "recipe", "recipe": self.recipe})

    async def play_radio(self, query: str) -> dict[str, Any] | None:
        stations = await search_stations(self.http, query)
        if stations:
            await self.play_station(stations[0])
            return stations[0]
        return None

    async def play_station(self, station: dict[str, Any]) -> None:
        """A radio station, or a library playlist played again."""
        if self.spotify.playing:
            await self.spotify.stop()
        await self.media.play(station)

    async def play_library(self, plan: Plan) -> None:
        if self.spotify.playing:
            await self.spotify.stop()
        await self.media.play(self.library.station(plan))

    async def _vacation_power(self, ids: list[int], on: bool) -> None:
        await self.devices.control(ids, "power", on)

    def schedules_payload(self) -> list[dict[str, Any]]:
        return [{**s, "description": describe(s)} for s in self.schedules.all()]

    async def announce(self, text: str) -> None:
        """Have taco-voice chime and speak `text` (alerts, scheduled reminders)."""
        await self.hub.broadcast({"type": "announce", "text": text})

    async def notify(self, kind: str, title: str, body: str = "", *, key: str | None = None,
                     sender: str | None = None, wake: bool = False, chime: bool = False,
                     merge_s: float | None = 120.0) -> dict[str, Any]:
        """Put a card on screen. `wake` lights the screen (even from night
        mode); `chime` has taco-voice play the announcement chime."""
        card = self.notifications.add(kind, title, body, key=key, sender=sender, merge_s=merge_s)
        await self.broadcast_notifications(wake=wake)
        if chime:
            await self.hub.broadcast({"type": "notification_chime"})
        if wake:
            await self.set_screen("on", broadcast=False)   # powers the panel back up
        return card

    async def broadcast_notifications(self, wake: bool = False) -> None:
        await self.hub.broadcast({"type": "notifications", "notifications": self.notifications.active(),
                                  "wake": wake})

    def quiet_now(self) -> bool:
        q = self.cfg["notifications"]["quiet_hours"]
        return bool(q) and in_window(datetime.now(), q["start"], q["end"])

    async def health_loop(self) -> None:
        """One card per problem. It updates in place if the problem comes back
        before it's dismissed, and says "back to normal" when it clears."""
        while True:
            try:
                readings = await self.health.sample()
                start, end = evaluate(readings, self.health.active, self.health.limits)
                for name in start:
                    self.health.active.add(name)
                    title, body = card(name, readings)
                    log.warning("health: %s", title)
                    await self.notify("health", title, body, key=f"health:{name}", merge_s=None)
                for name in end:
                    self.health.active.discard(name)
                    log.info("health: %s back to normal", name)
                    if self.notifications.resolve(f"health:{name}"):
                        await self.broadcast_notifications()
            except Exception:
                log.exception("health check failed")
            await asyncio.sleep(self.health.interval_s)

    def events_on(self, day) -> list[dict[str, Any]]:
        return [e for e in self.events if e["date"] == day.isoformat()]

    async def calendar_loop(self) -> None:
        urls = self.cfg["calendar"]["ical_urls"]
        if not urls:
            return
        while True:
            self.events = await fetch_events(self.http, urls, datetime.now().date())
            await self.hub.broadcast({"type": "events", "events": self.events_on(datetime.now().date())})
            await asyncio.sleep(CALENDAR_REFRESH_S)

    async def alerts_loop(self) -> None:
        while True:
            try:
                loc = self.db.settings()["location"]
                if loc:
                    self.alerts = await fetch_alerts(self.http, loc["lat"], loc["lon"])
                    await self.hub.broadcast({"type": "alerts", "alerts": self.alerts})
                    for a in self.alerts:
                        if a["announce"] and a["id"] not in self._announced_alerts:
                            self._announced_alerts.add(a["id"])
                            await self.announce(f"Weather alert. {a['headline']}.")
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("weather alerts refresh failed: %s", e)
            await asyncio.sleep(ALERTS_REFRESH_S)

    def lists_payload(self) -> dict[str, list[dict[str, Any]]]:
        return {name: self.lists.items(name) for name in self.lists.names()}

    async def broadcast_lists(self) -> None:
        await self.hub.broadcast({"type": "lists", "lists": self.lists_payload()})

    async def set_screen(self, mode: str, broadcast: bool = True) -> None:
        """mode: "night" (dark until touched) or "on". With night_screen_off set,
        the panel itself is powered down (DPMS via wlopm) rather than shown black."""
        if broadcast:
            await self.hub.broadcast({"type": "screen", "mode": mode})
        if self.db.settings()["night_screen_off"]:
            env = {**os.environ, "WAYLAND_DISPLAY": "wayland-0",
                   "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
            proc = await asyncio.create_subprocess_exec(
                "wlopm", "--off" if mode == "night" else "--on", "*", env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()

    async def run_scene(self, scene: dict[str, Any]) -> str:
        """Carry out a scene's actions; return what Taco should say."""
        speech, failed = await self.run_actions(scene["actions"])
        key = scene_key(scene["name"])
        if not speech:
            speech.append({"good night": "Good night!",
                           "i m leaving": "Goodbye! I've turned everything off."}.get(key, "Done."))
        if failed:
            speech.append(f"{', '.join(failed)} didn't respond.")
        return " ".join(speech)

    async def run_actions(self, actions: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """Run scene/schedule actions. Returns (things to say, devices that failed)."""
        speech: list[str] = []
        failed: list[str] = []
        for action in actions:
            kind = action.get("type")
            if kind == "devices":
                if "ids" in action:
                    ids = action["ids"]
                else:
                    m = self.devices.resolve(action.get("target", "everything"))
                    ids = m.ids if m else []
                if "level" in action:
                    _, bad = await self.devices.control(ids, "level", int(action["level"]))
                else:
                    _, bad = await self.devices.control(ids, "power", bool(action.get("on", True)))
                failed += [d["name"] for d in bad]
            elif kind == "briefing":
                speech.append(self.assistant.briefing())
            elif kind == "night":
                await self.set_screen("night")
            elif kind == "media_stop":
                await self.stop_music()
            elif kind == "scene" and (scene := self.scenes.get(action.get("name", ""))):
                more, bad = await self.run_actions(scene["actions"])
                speech += more
                failed += bad
            elif kind == "alarm":
                self.db.timers.add_alarm(time.time(), action.get("label"))
                await self.broadcast_timers()
            elif kind == "say" and action.get("text"):
                await self.announce(action["text"])
        return speech, failed


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
                "found": self.devices.found_payload(), "lists": self.lists_payload(),
                "scenes": self.scenes.all(), "schedules": self.schedules_payload(), "alerts": self.alerts,
                "media": self.media_status(), "recipe": self.recipe,
                "notifications": self.notifications.active(),
                "events": self.events_on(datetime.now().date())}

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

    async def publish_weather(self, force: bool = False) -> None:
        """Apply the latest station reading to the forecast; push it if it changed."""
        merged = apply_station(self.forecast, self.station_reading, self.db.settings()["units"],
                               self.cfg["weather_station"]["stale_after_s"])
        changed = merged is not None and (self.weather is None or merged["current"] != self.weather["current"])
        self.weather = merged
        if merged is not None and (force or changed):
            await self.hub.broadcast({"type": "weather", "weather": merged})

    async def station_loop(self) -> None:
        if self.station is None:
            return
        poll_s = self.cfg["weather_station"]["poll_s"]
        while True:
            try:
                self.station_reading = await self.station.read(self.http)
                if self.station_error:
                    log.info("weather station: back (%s)", self.station.name)
                self.station_error = None
            except (httpx.HTTPError, OAuthError, ValueError, KeyError) as e:
                if self.station_error is None:   # once per outage, not every poll
                    log.warning("weather station %s unavailable: %s", self.station.name, e)
                self.station_error = str(e) or type(e).__name__
            await self.publish_weather()   # also drops a reading once it goes stale
            await asyncio.sleep(poll_s)

    async def weather_loop(self) -> None:
        while True:
            delay = WEATHER_REFRESH_S
            try:
                await self.ensure_location()
                settings = self.db.settings()
                if settings["location"]:
                    self.forecast = await fetch_forecast(self.http, settings["location"], settings["units"])
                    await self.publish_weather(force=True)
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
    await state.speakers.reset()   # a Bluetooth speaker choice never outlives a restart
    tasks = [asyncio.create_task(c) for c in (
        state.weather_loop(), state.station_loop(), state.timer_loop(), state.matter.run(), state.devices.poll_loop(),
        state.scheduler.loop(), state.alerts_loop(), state.calendar_loop(),
        state.health_loop(), state.speakers.watch(), state.library.loop())]
    yield
    for task in tasks:
        task.cancel()
    await state.media.stop(notify=False)
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


@app.get("/api/station")
async def get_station() -> dict[str, Any]:
    """The local weather station's last reading, for checking it works."""
    if state.station is None:
        return {"provider": None}
    r = state.station_reading
    return {"provider": state.station.name, "error": state.station_error,
            "age_s": round(time.time() - r.measured_at) if r else None,
            "reading": dataclasses.asdict(r) if r else None}


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


# --- lists -------------------------------------------------------------------

@app.get("/api/lists")
async def get_lists() -> dict[str, list[dict[str, Any]]]:
    return state.lists_payload()


@app.post("/api/lists/{name}")
async def add_list_items(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """body: {"items": ["milk", "eggs"]}"""
    added = state.lists.add(name, [str(i) for i in body.get("items", [])])
    await state.broadcast_lists()
    return {"added": added}


@app.patch("/api/lists/items/{item_id}")
async def update_list_item(item_id: int, body: dict[str, Any]) -> dict[str, bool]:
    state.lists.set_done(item_id, bool(body.get("done", True)))
    await state.broadcast_lists()
    return {"ok": True}


@app.delete("/api/lists/items/{item_id}")
async def delete_list_item(item_id: int) -> dict[str, bool]:
    state.lists.delete(item_id)
    await state.broadcast_lists()
    return {"ok": True}


@app.delete("/api/lists/{name}")
async def clear_list(name: str, done_only: bool = False) -> dict[str, int]:
    removed = state.lists.clear(name, only_done=done_only)
    await state.broadcast_lists()
    return {"removed": removed}


# --- scenes ------------------------------------------------------------------

@app.get("/api/scenes")
async def list_scenes() -> list[dict[str, Any]]:
    return state.scenes.all()


@app.put("/api/scenes/{name}")
async def save_scene(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """body: {"actions": [...]} (see taco/core/scenes.py for action shapes)."""
    actions = body.get("actions")
    if not isinstance(actions, list) or not actions:
        raise HTTPException(400, "a scene needs a non-empty list of actions")
    scene = state.scenes.save(name, actions)
    await state.hub.broadcast({"type": "scenes", "scenes": state.scenes.all()})
    return scene


@app.delete("/api/scenes/{name}")
async def delete_scene(name: str) -> dict[str, bool]:
    if not state.scenes.delete(name):
        raise HTTPException(404, f"no scene {name!r}")
    await state.hub.broadcast({"type": "scenes", "scenes": state.scenes.all()})
    return {"ok": True}


@app.post("/api/scenes/{name}/run")
async def run_scene(name: str) -> dict[str, str]:
    scene = state.scenes.get(name)
    if scene is None:
        raise HTTPException(404, f"no scene {name!r}")
    return {"speech": await state.run_scene(scene)}


# --- schedules and alerts ----------------------------------------------------------

@app.get("/api/schedules")
async def list_schedules() -> list[dict[str, Any]]:
    return state.schedules_payload()


@app.post("/api/schedules")
async def add_schedule(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"name", "trigger": {"at": "HH:MM"} | {"sun": "sunset", "offset_min": -15},
    "days": [0-6, Monday = 0; empty = daily], "actions": [...]} (see scheduler.py)."""
    try:
        s = state.schedules.add(str(body["name"]), body["trigger"], list(body.get("days", [])),
                                list(body["actions"]))
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"bad schedule: {e}") from None
    await state.hub.broadcast({"type": "schedules", "schedules": state.schedules_payload()})
    return {**s, "description": describe(s)}


@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, body: dict[str, Any]) -> dict[str, bool]:
    changes = {k: v for k, v in body.items() if k in ("name", "trigger", "days", "actions", "enabled")}
    try:
        state.schedules.update(schedule_id, **changes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    await state.hub.broadcast({"type": "schedules", "schedules": state.schedules_payload()})
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int) -> dict[str, bool]:
    state.schedules.delete(schedule_id)
    await state.hub.broadcast({"type": "schedules", "schedules": state.schedules_payload()})
    return {"ok": True}


@app.get("/api/calendar")
async def calendar(days: int = 1) -> list[dict[str, Any]]:
    """Events from today for `days` days (up to 7)."""
    today = datetime.now().date()
    last = (today + timedelta(days=max(1, min(days, 7)) - 1)).isoformat()
    return [e for e in state.events if today.isoformat() <= e["date"] <= last]


@app.get("/api/alerts")
async def get_alerts() -> list[dict[str, Any]]:
    return state.alerts


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Latest Pi readings (CPU is the window average; None until the window fills)."""
    return state.health.latest


@app.post("/api/announce")
async def announce(body: dict[str, Any]) -> dict[str, bool]:
    """Speak a message out loud in the kitchen (intercom, reminders from Hermes)."""
    if not str(body.get("text", "")).strip():
        raise HTTPException(400, "nothing to say")
    await state.announce(str(body["text"]))
    return {"ok": True}


# --- notifications -------------------------------------------------------------

@app.get("/api/notifications")
async def list_notifications() -> list[dict[str, Any]]:
    return state.notifications.active()


@app.post("/api/notifications")
async def add_notification(body: dict[str, Any]) -> dict[str, Any]:
    """Local senders (scripts, Hermes): {"title", "body"?, "kind"?, "key"?, "wake"?, "chime"?}."""
    try:
        n = parse_payload(body)
    except Rejected:
        raise HTTPException(400, "a notification needs a title") from None
    return await state.notify(n["kind"], n["title"], n["body"], key=n["key"], wake=n["wake"], chime=n["chime"])


@app.delete("/api/notifications/{card_id}")
async def dismiss_notification(card_id: int) -> dict[str, bool]:
    state.notifications.dismiss(card_id)
    await state.broadcast_notifications()
    return {"ok": True}


@app.delete("/api/notifications")
async def dismiss_all_notifications() -> dict[str, int]:
    removed = state.notifications.dismiss_all()
    await state.broadcast_notifications()
    return {"removed": removed}


@app.api_route("/hook/notify", methods=["GET", "POST"])
@app.api_route("/hook/notify/{token}", methods=["GET", "POST"])
async def webhook_notify(request: Request, token: str | None = None) -> dict[str, bool]:
    """Notifications from outside senders through the Cloudflare tunnel (webhook.py).
    The secret goes in X-Taco-Token, or in the path for senders that can't set
    headers. Fields come from the JSON body or, for a GET, the query string."""
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    try:
        sender = state.webhook.check(ip, request.headers.get("x-taco-token") or token)
        data: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            raw = await request.body()
            if len(raw) > MAX_BODY_BYTES:
                raise Rejected(413)
            if raw:
                try:
                    posted = json.loads(raw)
                except ValueError:
                    raise Rejected(400) from None
                if not isinstance(posted, dict):
                    raise Rejected(400)
                data.update(posted)
        n = parse_payload(data)
    except Rejected as r:
        raise HTTPException(r.status, "Not Found" if r.status == 404 else None) from None
    log.info("webhook notification from %s: %r", sender, n["title"])
    await state.notify(n["kind"], n["title"], n["body"], sender=sender,
                       key=f"{sender}:{n['key']}" if n["key"] else None,
                       wake=n["wake"], chime=n["chime"] and not state.quiet_now())
    # Pass it on to Chilli (Telegram) in the background; a slow or failed send never delays the hook.
    task = asyncio.create_task(forward(state.http, state.cfg["notifications"].get("forward", {}), n, sender))
    state._forwards.add(task)
    task.add_done_callback(state._forwards.discard)
    return {"ok": True}


# --- radio ---------------------------------------------------------------------

@app.get("/api/media")
async def media_status() -> dict[str, Any] | None:
    return state.media_status()


@app.post("/api/media/spotify")
async def spotify_event(event: dict[str, Any]) -> dict[str, bool]:
    """Playback events from librespot's --onevent hook (deploy/spotify-event.sh)."""
    if event.get("event") in ("playing", "started") and state.media.playing:
        await state.media.stop(notify=False)   # Spotify takes over from the radio
    state.spotify.update(event)
    await state.broadcast_media()
    return {"ok": True}


@app.post("/api/media/play")
async def play_media(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"query": "WBUR" | "jazz"} or {"name", "url"} of a station."""
    try:
        if body.get("url"):
            station = {"name": body.get("name") or "Radio", "url": body["url"]}
            await state.media.play(station)
        elif not (station := await state.play_radio(str(body.get("query", "")))):
            raise HTTPException(404, f"no station found for {body.get('query')!r}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"station search failed: {e}") from None
    return station


@app.post("/api/media/stop")
async def stop_media() -> dict[str, bool]:
    await state.stop_music()
    return {"ok": True}


# --- music library on the NAS (taco/core/library.py) ------------------------------

@app.get("/api/music/status")
async def music_status() -> dict[str, Any]:
    return state.library.status()


@app.get("/api/music/search")
async def music_search(q: str) -> dict[str, Any]:
    return await asyncio.to_thread(state.library.search, q)


@app.post("/api/music/play")
async def music_play(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"artist"?, "album"?, "title"?}: an artist plays shuffled, an album
    in order, a title as one song."""
    artist, album, title = body.get("artist"), body.get("album"), body.get("title")
    lib = state.library
    if title:
        plan = await asyncio.to_thread(lib.resolve, title, artist, None)
    elif album:
        plan = await asyncio.to_thread(lib.resolve, album, artist, "album")
    elif artist:
        plan = await asyncio.to_thread(lib.resolve, artist, None, "artist")
    else:
        raise HTTPException(400, "give an artist, album or title")
    if plan is None:
        raise HTTPException(404, "not found in the music library; try search_music")
    await state.play_library(plan)
    return {"playing": plan.name, "kind": plan.kind, "songs": len(plan.paths)}


@app.post("/api/music/reindex")
async def music_reindex() -> dict[str, Any]:
    """Start a re-index in the background; the current index stays in use."""
    if not state.library.enabled:
        raise HTTPException(503, "no music library configured")
    started = not state.library.indexing
    if started:
        state.reindex_task = asyncio.create_task(state.library.reindex())   # keep a reference
    return {"started": started}


# --- speakers (where music plays; taco/core/speakers.py) -------------------------

@app.get("/api/speakers")
async def list_speakers() -> dict[str, Any]:
    found = await state.speakers.list()
    current = state.speakers.current
    return {"speakers": [KITCHEN.name, *[s.name for s in found]],
            "current": current.name if current else KITCHEN.name}


@app.post("/api/speakers/select")
async def select_speaker(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"name": "Sonos"}; a null name means the kitchen speaker."""
    found = await state.speakers.list()
    chosen = resolve(body.get("name"), found)
    if chosen is None:
        known = ", ".join([KITCHEN.name, *[s.name for s in found]])
        raise HTTPException(404, f"no speaker called {body.get('name')!r}; known: {known}")
    if not await state.speakers.select(chosen):
        raise HTTPException(502, f"couldn't connect to {chosen.name}; is it on?")
    return {"speaker": chosen.name}


@app.post("/api/speakers/pair")
async def pair_speaker() -> dict[str, Any]:
    """Scan ~30 s for a speaker in pairing mode, then pair with it."""
    found = await state.speakers.pair()
    return {"paired": found.name if found else None}


@app.get("/api/media/search")
async def search_media(q: str) -> list[dict[str, Any]]:
    try:
        return await search_stations(state.http, q)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"station search failed: {e}") from None


# --- recipe mode -------------------------------------------------------------------

@app.get("/api/recipe")
async def get_recipe() -> dict[str, Any] | None:
    return state.recipe


@app.put("/api/recipe")
async def show_recipe(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"title", "ingredients": [...], "steps": [...], "servings"?}. Opens on screen."""
    steps = [str(x) for x in body.get("steps", []) if str(x).strip()]
    if not body.get("title") or not steps:
        raise HTTPException(400, "a recipe needs a title and steps")
    state.recipe = {"title": str(body["title"]), "servings": body.get("servings"),
                    "ingredients": [str(x) for x in body.get("ingredients", [])], "steps": steps, "step": 0}
    await state.broadcast_recipe()
    return state.recipe


@app.post("/api/recipe/step")
async def recipe_step(body: dict[str, Any]) -> dict[str, Any]:
    """body: {"delta": 1 | -1} or {"index": n} (0-based)."""
    if state.recipe is None:
        raise HTTPException(404, "no recipe open")
    n = len(state.recipe["steps"])
    target = int(body["index"]) if "index" in body else state.recipe["step"] + int(body.get("delta", 1))
    state.recipe["step"] = max(0, min(n - 1, target))
    await state.broadcast_recipe()
    return state.recipe


@app.delete("/api/recipe")
async def close_recipe() -> dict[str, bool]:
    state.recipe = None
    await state.broadcast_recipe()
    return {"ok": True}


@app.post("/api/screen")
async def screen(body: dict[str, Any]) -> dict[str, bool]:
    """body: {"mode": "night" | "on", "from_ui": bool}. The UI reports its own
    night/wake changes with from_ui so they aren't echoed back to it."""
    await state.set_screen(str(body.get("mode", "on")), broadcast=not body.get("from_ui"))
    return {"ok": True}


@app.post("/api/voice/event")
async def voice_event(event: dict[str, Any]) -> dict[str, bool]:
    """State updates from taco-voice: listening, heard text, tool use, reply."""
    if event.get("type") == "state":
        state.voice = {"state": event.get("state", "idle")}
        # Turn music down while Taco is listening or talking.
        ducked = state.voice["state"] != "idle"
        await state.media.duck(ducked)
        if state.spotify.playing:
            await state.spotify.duck(ducked)
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
    logging.getLogger("httpx").setLevel(logging.WARNING)   # a music index makes ~45,000 requests
    core = load_config()["core"]
    uvicorn.run(app, host=core["host"], port=core["port"], log_level="warning")


if __name__ == "__main__":
    main()
