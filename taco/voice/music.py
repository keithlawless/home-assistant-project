"""Music on the satellites' speakers.

taco-core decides which rooms music plays in and sends taco-voice a
`music_route` (the satellite rooms). This captures what plays into the
taco_music sink (radio, the NAS, Spotify) from its monitor, mono at 48 kHz,
and sends it down each of those satellites' connections as MUSIC frames,
which the board mixes with Taco's replies.

It only runs pw-record while some satellite is in the route, and sends
nothing while the music is silent, so the boards' amps switch off. Each
board gets a short cushion of silence before its music starts, so network
jitter doesn't empty its buffer. The capture follows the music's real-time
clock, so the board's buffer holds steady (apart from the two clocks' drift,
a few seconds a day).
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from .audio import resample

log = logging.getLogger(__name__)

RATE = 48000
CHUNK_S = 0.1
CUSHION_S = 0.3
SILENT_S = 1.0   # this long at digital silence: stop sending until music comes back
SILENT_CHUNKS = round(SILENT_S / CHUNK_S)
CAPTURE = ["pw-record", "--raw", "--target", "taco_music",
           "-P", "{ stream.capture.sink = true, node.name = taco_music_satellites, state.restore-props = false }",
           "--rate", str(RATE), "--channels", "1", "--format", "s16", "--latency", "50ms", "-"]


class SatelliteMusic:
    def __init__(self, satellites: Callable[[], list[Any]], capture: list[str] | None = None):
        """`satellites` gives the satellite connections (NetworkMicrophone);
        each has name, connected, speaker, speaker_rate, send_audio and
        send_line."""
        self._satellites = satellites
        self._capture = capture or CAPTURE
        self._rooms: set[str] = set()
        self._primed: set[str] = set()   # rooms whose board has its cushion
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._proc: subprocess.Popen | None = None
        threading.Thread(target=self._run, daemon=True, name="sat-music").start()

    @property
    def rooms(self) -> set[str]:
        return set(self._rooms)

    def set_rooms(self, rooms: list[str] | set[str]) -> None:
        new = set(rooms)
        with self._lock:
            gone, self._rooms = self._rooms - new, new
            self._primed -= gone
        for sat in self._satellites():
            if sat.name in gone:
                sat.send_line("MSTOP")   # drop what it has queued
        if gone or new:
            log.info("music on satellites: %s", ", ".join(sorted(new)) or "none")
        if new:
            self._wake.set()
        elif self._proc:
            self._proc.terminate()   # the reader ends and the loop waits again

    def reconnected(self, name: str) -> None:
        """A satellite's connection is new: its buffer is empty, so cushion it again."""
        self._primed.discard(name)

    def _targets(self) -> list[Any]:
        with self._lock:
            rooms = set(self._rooms)
        return [s for s in self._satellites() if s.name in rooms and s.connected and s.speaker]

    def _send(self, pcm: np.ndarray) -> None:
        for sat in self._targets():
            rate = getattr(sat, "speaker_rate", RATE)
            if sat.name not in self._primed:
                cushion = np.zeros(int(rate * CUSHION_S), dtype=np.int16)
                if not sat.send_audio(cushion.tobytes(), kind=b"MUSIC"):
                    continue
                self._primed.add(sat.name)
            out = pcm if rate == RATE else resample(pcm, RATE, rate)
            if not sat.send_audio(out.tobytes(), kind=b"MUSIC"):
                self._primed.discard(sat.name)   # disconnected: cushion again when it's back

    def _run(self) -> None:
        chunk_bytes = int(RATE * CHUNK_S) * 2
        while True:
            self._wake.wait()
            self._wake.clear()
            if not self._rooms:
                continue
            try:
                self._proc = subprocess.Popen(self._capture, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as e:
                log.warning("can't capture music for the satellites: %s", e)
                continue
            silent = 0
            try:
                while self._rooms:
                    data = self._proc.stdout.read(chunk_bytes)
                    if len(data) < chunk_bytes:
                        break
                    pcm = np.frombuffer(data, dtype=np.int16)
                    if not pcm.any():
                        silent += 1
                        if silent > SILENT_CHUNKS:
                            self._primed.clear()   # cushion again when music returns
                            continue
                    else:
                        silent = 0
                    self._send(pcm)
            finally:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None
            if self._rooms:
                log.warning("music capture for the satellites stopped; restarting")
                self._wake.set()
                threading.Event().wait(1.0)
