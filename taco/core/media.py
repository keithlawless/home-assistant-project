"""Internet radio through mpv, with station search from Radio Browser
(radio-browser.info, a free community directory). Music is "ducked" to a
low volume while Taco is listening or talking."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

RADIO_BROWSER = "https://all.api.radio-browser.info/json"
HEADERS = {"User-Agent": "Taco/0.1 (kitchen assistant)"}
SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "taco-mpv.sock")
DUCK_VOLUME = 20
NORMAL_VOLUME = 100


async def search_stations(http: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Best matches by name, then by genre tag, most popular first."""
    params = {"limit": 5, "hidebroken": "true", "order": "clickcount", "reverse": "true"}
    out: list[dict[str, Any]] = []
    for field in ("name", "tag"):
        r = await http.get(f"{RADIO_BROWSER}/stations/search", params={**params, field: query},
                           headers=HEADERS)
        r.raise_for_status()
        out += [s for s in r.json() if s.get("url_resolved")]
        if out:
            break
    return [{"name": s["name"].strip(), "url": s["url_resolved"], "tags": s.get("tags", ""),
             "country": s.get("countrycode", "")} for s in out]


class RadioPlayer:
    def __init__(self, output_device: str | None, on_change):
        self._device = output_device
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
