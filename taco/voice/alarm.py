"""Plays ringing timers, alarms and reminders until they're stopped.

taco-core owns timer state; this just mirrors which ones are ringing. Each
newly ringing item is announced by voice ("Your pasta timer is done"),
then a chime repeats, with the announcement again every minute.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .audio import tone

log = logging.getLogger(__name__)

CHIME = np.concatenate([tone([784, 988, 1175], duration=0.14, volume=0.45),
                        np.zeros(int(22050 * 0.25), dtype=np.int16)])
CHIME_RATE = 22050
REANNOUNCE_S = 60


def announcement(timer: dict[str, Any]) -> str:
    if timer["kind"] == "reminder":
        return f"Reminder: {timer['message']}."
    if timer["kind"] == "alarm":
        return f"Your {timer['label']} alarm is going off." if timer["label"] else "Your alarm is going off."
    return f"Your {timer['label']} timer is done." if timer["label"] else "Your timer is done."


class AlarmPlayer:
    """Rings each item in the room it was set from: `speaker_for(room)` gives
    that room's speaker (a satellite's, or the kitchen's when the room has
    none or its satellite is offline). It's asked again on every chime, so a
    satellite dropping mid-ring falls back to the kitchen."""

    def __init__(self, speaker_for: Callable[[str | None], Any],
                 synthesize: Callable[[str], tuple[np.ndarray, int]]):
        self._speaker_for = speaker_for
        self._synthesize = synthesize
        self._ringing: dict[int, dict[str, Any]] = {}
        self._announced: dict[int, float] = {}
        self._used: set[Any] = set()   # speakers this ring has played on
        self._lock = threading.Lock()
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="alarm").start()

    @property
    def active(self) -> bool:
        return bool(self._ringing)

    def update(self, timers: list[dict[str, Any]]) -> None:
        with self._lock:
            was = bool(self._ringing)
            self._ringing = {t["id"]: t for t in timers if t["state"] == "ringing"}
            self._announced = {k: v for k, v in self._announced.items() if k in self._ringing}
        if self._ringing:
            self._wake.set()
        elif was:
            # Only when ringing ends: a satellite's speaker also plays replies,
            # and every timer change (like "timer set") arrives here.
            self._stop_all()

    def silence(self) -> None:
        with self._lock:
            self._ringing = {}
        self._stop_all()

    def _stop_all(self) -> None:
        with self._lock:
            used, self._used = self._used, set()
        for speaker in used:
            speaker.stop()

    def _speaker(self, timer: dict[str, Any]) -> Any:
        speaker = self._speaker_for(timer.get("room"))
        with self._lock:
            self._used.add(speaker)
        return speaker

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            while self.active:
                now = time.monotonic()
                with self._lock:
                    ringing = list(self._ringing.values())
                    due = [t for t in ringing if now - self._announced.get(t["id"], -1e9) >= REANNOUNCE_S]
                    for t in due:
                        self._announced[t["id"]] = now
                for t in due:
                    log.info("announcing in %s: %s", t.get("room") or "the kitchen", announcement(t))
                    self._speaker(t).play(*self._synthesize(announcement(t)))
                if self.active:
                    speakers = list(dict.fromkeys(self._speaker(t) for t in ringing))
                    for speaker in speakers:
                        speaker.play(CHIME, CHIME_RATE)
                    for speaker in speakers:
                        speaker.wait()
