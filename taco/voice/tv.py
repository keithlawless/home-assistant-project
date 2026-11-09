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

Pause ads (DirecTV, measured 2026-11-09): about 25 s into a pause the app
plays an ad, which the Apple TV reports as the program Playing again, with
the same title, hash and position. On that app "play" acts as a toggle: over
the ad it returns to the paused program, and on the playing program it
pauses. So at the end of a long turn a switch to Playing that came at least
pause_ad_after_s after Taco's pause counts as an ad. Taco sends play, and
play again once the TV is back to Paused. A switch to Playing sooner than
that is someone pressing play, and is left alone.
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
    def __init__(self, host: str, rooms: list[str], marker: str | Path, resume_within_s: float = 600,
                 pause_ad_after_s: float = 20):
        self.host = host
        self.rooms = set(rooms)
        self.marker = Path(marker)
        self.resume_within_s = resume_within_s
        self.pause_ad_after_s = pause_ad_after_s
        self._loop = asyncio.new_event_loop()
        self._atv = None
        self._connected = asyncio.Event()
        # All owned by the asyncio thread:
        self._paused_by_us = False
        self._paused_at: float | None = None      # monotonic time of Taco's pause
        self._playing_since: float | None = None  # last switch to Playing, from push updates
        self._last_state: str | None = None

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
        pauser = self

        class Push:   # live play-state updates, to time a switch to Playing
            def playstatus_update(self, updater, playing):
                pauser._on_state(playing.device_state.name)

            def playstatus_error(self, updater, exception):
                pass

        atv.push_updater.listener = Push()
        atv.push_updater.start()
        self._atv = atv
        self._connected.set()
        log.info("apple tv %s: connected", self.host)

    async def _ready(self):
        try:
            await asyncio.wait_for(self._connected.wait(), 2.0)
        except asyncio.TimeoutError:
            return None
        return self._atv

    async def _state(self, atv, settle_s: float = 0.0) -> str:
        """Playing, Paused, Idle... Right after connecting, the Apple TV can
        say Idle until its first update arrives; settle_s waits that out."""
        end = time.monotonic() + settle_s
        while True:
            state = (await atv.metadata.playing()).device_state.name
            if state not in ("Idle", "Loading") or time.monotonic() >= end:
                return state
            await asyncio.sleep(0.25)

    def _on_state(self, state: str) -> None:
        if state == "Playing" and self._last_state != "Playing":
            self._playing_since = time.monotonic()
        self._last_state = state

    async def _wait_for(self, atv, state: str, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if await self._state(atv) == state:
                return True
            await asyncio.sleep(0.25)
        return False

    def _pause_ad_likely(self) -> bool:
        """Playing again, long enough after Taco's pause to be a pause ad
        rather than someone pressing play."""
        return (self._paused_at is not None and self._playing_since is not None
                and self._playing_since - self._paused_at >= self.pause_ad_after_s)

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
            self._paused_at, self._playing_since = time.monotonic(), None
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
            state = await self._state(atv, settle_s=5.0)
            if state == "Paused":
                await atv.remote_control.play()
                log.info("apple tv %s: resumed", self.host)
            elif state == "Playing" and self._pause_ad_likely():
                # Play dismisses the ad back to the paused program; play again resumes it.
                await atv.remote_control.play()
                if await self._wait_for(atv, "Paused", 3.0):
                    await atv.remote_control.play()
                log.info("apple tv %s: resumed past a pause ad (playing again %.0f s after the pause)",
                         self.host, self._playing_since - self._paused_at)
            else:
                log.info("apple tv %s: not resuming, it's %s now", self.host, state)
        except Exception as e:
            log.info("apple tv %s: resume failed: %s", self.host, e)
            return
        self._paused_by_us = False
        self._paused_at = self._playing_since = None
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
