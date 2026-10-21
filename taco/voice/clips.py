"""Short recordings of wake word events, kept for tuning and retraining.

Each event is a 16 kHz mono WAV plus a JSON file with the same name holding
the scores, under <clips_dir>/wake/ and <clips_dir>/near_miss/:

- wake: the ~2.4 s before the detection, then the request that followed
  (from `preroll_s` on). `heard` is the transcript, which makes TV false
  triggers easy to pick out.
- near_miss: the ~2.4 s around a score that came close without firing, which
  is what a missed "Hey Taco" looks like.

Files are written on a background thread, and the oldest are deleted once a
folder holds more than max_clips.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class ClipRecorder:
    def __init__(self, directory: str | Path, max_clips: int = 1000, near_miss_gap_s: float = 10.0):
        self.dir = Path(directory)
        self.max_clips = max_clips
        self.near_miss_gap_s = near_miss_gap_s   # TV audio can come close often
        self._last_near: dict[str, float] = {}
        self._q: queue.Queue[tuple[str, str, np.ndarray, dict]] = queue.Queue(maxsize=20)
        threading.Thread(target=self._run, daemon=True, name="clips").start()

    def save(self, kind: str, room: str, audio: np.ndarray, info: dict) -> None:
        if kind == "near_miss":
            now = time.monotonic()
            if now - self._last_near.get(room, -1e9) < self.near_miss_gap_s:
                return
            self._last_near[room] = now
        try:
            self._q.put_nowait((kind, room, audio, info))
        except queue.Full:
            pass   # disk is behind; losing a clip is fine

    def wait(self) -> None:
        """Block until queued clips are written (tests)."""
        self._q.join()

    def _run(self) -> None:
        while True:
            kind, room, audio, info = self._q.get()
            try:
                self._write(kind, room, audio, info)
            except Exception:
                log.exception("could not save %s clip", kind)
            finally:
                self._q.task_done()

    def _write(self, kind: str, room: str, audio: np.ndarray, info: dict) -> None:
        folder = self.dir / kind
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        stem = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}_{re.sub(r'[^a-z0-9]+', '-', room.lower())}"
        n = 1
        while (folder / f"{stem}.wav").exists():   # same millisecond (tests)
            stem, n = f"{stem.rsplit('~', 1)[0]}~{n}", n + 1
        with wave.open(str(folder / f"{stem}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(audio.astype(np.int16).tobytes())
        meta = {"time": now.isoformat(timespec="seconds"), "kind": kind, "room": room, **info}
        (folder / f"{stem}.json").write_text(json.dumps(meta, indent=1) + "\n")
        log.info("saved %s clip %s", kind, stem)
        for old in sorted(folder.glob("*.wav"))[:-self.max_clips]:
            old.unlink(missing_ok=True)
            old.with_suffix(".json").unlink(missing_ok=True)
