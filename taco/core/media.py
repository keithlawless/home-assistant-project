"""Music: internet radio through mpv (stations from Radio Browser, a free
community directory) and a Spotify Connect speaker (librespot, see
deploy/systemd/taco-spotify.service). Both are "ducked" to a low volume
while Taco is listening or talking.

Both play into the taco_music loopback sink (deploy/pipewire/), whose output
speakers.py can point at a Bluetooth speaker. Taco's own voice doesn't go
through it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

# Radio Browser is run by volunteers on a few servers; "all" picks one by DNS,
# and a single server being down (503) shouldn't stop the radio.
RADIO_BROWSERS = ["https://all.api.radio-browser.info/json", "https://de1.api.radio-browser.info/json",
                  "https://de2.api.radio-browser.info/json"]
HEADERS = {"User-Agent": "Taco/0.1 (kitchen assistant)"}
SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "taco-mpv.sock")
DUCK_VOLUME = 20
NORMAL_VOLUME = 100
MUSIC_SINK = "taco_music"   # deploy/pipewire/60-taco-music.conf


async def run(*cmd: str, timeout: float | None = None) -> str:
    """Run a command and return its stdout ("" if it timed out)."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return out.decode(errors="replace")


async def find_node(match: Callable[[dict[str, Any]], bool]) -> tuple[int, dict[str, Any]] | None:
    """The first PipeWire node whose props satisfy `match`: (id, props)."""
    try:
        objects = json.loads(await run("pw-dump") or "[]")
    except ValueError:
        return None
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if obj.get("type") == "PipeWire:Interface:Node" and match(props):
            return obj["id"], props
    return None


async def search_stations(http: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Best matches by name, then by genre tag, most popular first."""
    params = {"limit": 5, "hidebroken": "true", "order": "clickcount", "reverse": "true"}
    out: list[dict[str, Any]] = []
    for field in ("name", "tag"):
        out += [s for s in await _radio_browser(http, {**params, field: query}) if s.get("url_resolved")]
        if out:
            break
    return [{"name": s["name"].strip(), "url": s["url_resolved"], "tags": s.get("tags", ""),
             "country": s.get("countrycode", "")} for s in out]


async def _radio_browser(http: httpx.AsyncClient, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Station search, trying the next server when one is down."""
    for i, base in enumerate(RADIO_BROWSERS):
        try:
            r = await http.get(f"{base}/stations/search", params=params, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if i == len(RADIO_BROWSERS) - 1 or (
                    isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500):
                raise
            log.warning("radio directory %s failed (%s); trying another server", base, e)
    return []


class RadioPlayer:
    def __init__(self, output_device: str | None, on_change):
        self._device = output_device   # normally MUSIC_SINK
        self._on_change = on_change
        self._proc: asyncio.subprocess.Process | None = None
        self.station: dict[str, Any] | None = None
        self._ducked = False

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict[str, Any] | None:
        return {"station": self.station["name"]} if self.playing and self.station else None

    async def play(self, station: dict[str, Any]) -> None:
        await self.stop(notify=False)
        args = ["mpv", "--no-video", "--really-quiet", "--no-terminal", f"--input-ipc-server={SOCKET}",
                f"--volume={DUCK_VOLUME if self._ducked else NORMAL_VOLUME}", "--cache=yes"]
        if self._device:
            args.append(f"--audio-device=pipewire/{self._device}")
        self._proc = await asyncio.create_subprocess_exec(*args, station["url"],
                                                          stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
        self.station = station
        log.info("playing %s", station["name"])
        asyncio.create_task(self._watch(self._proc))
        await self._on_change()

    async def _watch(self, proc: asyncio.subprocess.Process) -> None:
        await proc.wait()
        if proc is self._proc:   # ended on its own (stream dropped)
            log.info("radio stopped (exit %s)", proc.returncode)
            self._proc = None
            await self._on_change()

    async def stop(self, notify: bool = True) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
        if notify:
            await self._on_change()

    async def duck(self, ducked: bool) -> None:
        if ducked == self._ducked:
            return
        self._ducked = ducked
        if self.playing:
            await self._ipc({"command": ["set_property", "volume", DUCK_VOLUME if ducked else NORMAL_VOLUME]})

    async def _ipc(self, message: dict[str, Any]) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(SOCKET)
            writer.write((json.dumps(message) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), 2)
            writer.close()
        except (OSError, asyncio.TimeoutError) as e:
            log.debug("mpv ipc: %s", e)


class SpotifyState:
    """Mirrors librespot's playback events; ducks its PipeWire stream."""

    APP_NAME = "TacoSpotify"   # matches -P {application.name=...} in the service

    def __init__(self):
        self.playing = False
        self.track: dict[str, str] = {}

    def update(self, event: dict[str, Any]) -> None:
        name = event.get("event", "")
        if event.get("name"):
            self.track = {"name": event["name"], "artists": event.get("artists", "")}
        if name in ("playing", "started", "track_changed"):
            self.playing = True
        elif name in ("paused", "stopped", "session_disconnected", "end_of_track"):
            self.playing = False

    def status(self) -> dict[str, Any] | None:
        if not self.playing or not self.track:
            return None
        who = f" · {self.track['artists']}" if self.track.get("artists") else ""
        return {"station": f"{self.track['name']}{who}", "source": "spotify"}

    async def duck(self, ducked: bool) -> None:
        node = await find_node(lambda props: props.get("application.name") == self.APP_NAME)
        if node is not None:
            await run("wpctl", "set-volume", str(node[0]), "0.2" if ducked else "1.0")

    async def stop(self) -> None:
        """librespot has no local pause control; restarting it ends the session."""
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", "restart", "taco-spotify",
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        self.playing = False
