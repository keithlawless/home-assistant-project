"""Pause the living-room TV (an Apple TV, via pyatv) while Taco talks there.

A turn that Taco takes from a mic in one of `[tv] rooms` pauses the Apple TV,
but only if it is playing, and resumes it when the turn ends. Whatever was
already paused stays paused. Nothing here ever holds up the voice loop:
commands go to pyatv's own asyncio thread, and a TV that's asleep, missing or
unpaired just gets a log line.

Why pause and not mute: the Apple TV can't set the TV's volume to a level
(CEC gives it only relative up/down), and pausing means nothing is missed.

The Apple TV must be paired once (Companion and AirPlay) with pyatv's
`atvremote ... pair`, which keeps the credentials in ~/.pyatv.conf (see
README). A marker file remembers a pause that Taco made, so if taco-voice
restarts mid-turn it resumes the TV at startup. That happens only if the
pause is recent, so the TV doesn't start up by surprise hours later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

RECONNECT_S = (5, 15, 60, 300)   # backoff while the TV is off or unreachable


class TvPauser:
    def __init__(self, host: str, rooms: list[str], marker: str | Path, resume_within_s: float = 600):
        self.host = host
        self.rooms = set(rooms)
        self.marker = Path(marker)
        self.resume_within_s = resume_within_s
        self._loop = asyncio.new_event_loop()
        self._atv = None
        self._connected = asyncio.Event()
        self._paused_by_us = False   # owned by the asyncio thread

    def start(self) -> None:
        threading.Thread(target=self._loop.run_forever, daemon=True, name="tv").start()
        asyncio.run_coroutine_threadsafe(self._run(), self._loop)

    # -- called from the voice loop (never blocks) -----------------------------

    def turn_started(self, room: str) -> None:
        if room in self.rooms:
            asyncio.run_coroutine_threadsafe(self._pause(), self._loop)

    def turn_ended(self) -> None:
        asyncio.run_coroutine_threadsafe(self._resume(), self._loop)

    # -- the asyncio side ---------------------------------------------------------

    async def _run(self) -> None:
        if self._marker_age() is not None:
            # A restart mid-turn: resume what we paused, if it was just now.
            self._paused_by_us = self._marker_age() < self.resume_within_s
            if not self._paused_by_us:
                self._clear_marker()
        tries = 0
        while True:
            try:
                await self._connect()
                tries = 0
                if self._paused_by_us:
                    await self._resume()
                await self._lost.wait()
            except Exception as e:   # unreachable, asleep, not paired...
                log.info("apple tv %s: not connected (%s)", self.host, e or type(e).__name__)
            self._atv, self._connected = None, asyncio.Event()
            await asyncio.sleep(RECONNECT_S[min(tries, len(RECONNECT_S) - 1)])
            tries += 1

    async def _connect(self) -> None:
        import pyatv
        from pyatv.storage.file_storage import FileStorage

        storage = FileStorage.default_storage(self._loop)
        await storage.load()
        confs = await pyatv.scan(self._loop, hosts=[self.host], storage=storage, timeout=5)
        if not confs:
            raise ConnectionError("no answer")
        atv = await pyatv.connect(confs[0], self._loop, storage=storage)
        self._lost = asyncio.Event()
        lost = self._lost

        class Listener:
            def connection_lost(self, exception):
                lost.set()

            def connection_closed(self):
                lost.set()

        atv.listener = Listener()
        self._atv = atv
        self._connected.set()
        log.info("apple tv %s: connected", self.host)

    async def _ready(self):
        try:
            await asyncio.wait_for(self._connected.wait(), 2.0)
        except asyncio.TimeoutError:
            return None
        return self._atv

    async def _state(self, atv) -> str:
        return (await atv.metadata.playing()).device_state.name   # Playing, Paused, Idle...

    async def _pause(self) -> None:
        if self._paused_by_us:
            return   # a follow-up in the same turn: it's ours already
        atv = await self._ready()
        if atv is None:
            log.info("apple tv %s: not connected; not pausing", self.host)
            return
        try:
            if await self._state(atv) != "Playing":
                return   # paused or idle already: leave it alone
            await atv.remote_control.pause()
            self._paused_by_us = True
            self._write_marker()
            log.info("apple tv %s: paused for the turn", self.host)
        except Exception as e:
            log.info("apple tv %s: pause failed: %s", self.host, e)

    async def _resume(self) -> None:
        if not self._paused_by_us:
            return
        atv = await self._ready()
        if atv is None:
            return   # resumed after reconnecting (see _run), if still recent
        try:
            # Someone may have pressed play, or stopped it, meanwhile.
            if await self._state(atv) == "Paused":
                await atv.remote_control.play()
                log.info("apple tv %s: resumed", self.host)
        except Exception as e:
            log.info("apple tv %s: resume failed: %s", self.host, e)
            return
        self._paused_by_us = False
        self._clear_marker()

    def _marker_age(self) -> float | None:
        try:
            return time.time() - json.loads(self.marker.read_text())["paused_at"]
        except (OSError, ValueError, KeyError):
            return None

    def _write_marker(self) -> None:
        try:
            self.marker.write_text(json.dumps({"paused_at": time.time()}))
        except OSError as e:
            log.info("apple tv: can't write %s: %s", self.marker, e)

    def _clear_marker(self) -> None:
        self.marker.unlink(missing_ok=True)
