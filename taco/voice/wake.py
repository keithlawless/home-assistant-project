"""Wake word detection (openWakeWord, ONNX backend).

The model scores every 80 ms frame. A detection needs `patience` frames in a
row at or above the threshold (the Settings sensitivity plus a per-room
offset), so a brief spike from TV audio doesn't wake Taco. A run of scores
that comes close without firing is kept as a near miss, so missed wakes can
be saved and studied (see clips.py).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

HISTORY_FRAMES = 30   # 2.4 s of recent audio, kept for clips


@dataclass
class NearMiss:
    peak: float
    frames: int           # longest run at or above the threshold (< patience)
    audio: np.ndarray
    scores: list[float]


class Trigger:
    """Turns per-frame scores into detections. Subclasses supply _predict."""

    def __init__(self, sensitivity: float = 0.5, offset: float = 0.0, patience: int = 1,
                 near_miss: float | None = None):
        self.sensitivity = sensitivity   # from Settings
        self.offset = offset             # per room
        self.patience = max(1, patience)
        self.near_miss = near_miss       # report runs peaking at or above this; None = off
        self._history: deque[tuple[np.ndarray, float]] = deque(maxlen=HISTORY_FRAMES)
        self.reset()

    @property
    def threshold(self) -> float:
        return min(self.sensitivity + self.offset, 0.99)

    def _predict(self, frame: np.ndarray) -> float:
        raise NotImplementedError

    def detect(self, frame: np.ndarray) -> float | None:
        """Feed one 80 ms frame; return the peak score once the wake word fires."""
        score = float(self._predict(frame))   # the model gives np.float32; clips need JSON
        self._history.append((frame, score))
        threshold = self.threshold
        floor = threshold if self.near_miss is None else min(self.near_miss, threshold)
        if score >= floor:
            self._peak = max(self._peak, score)
            self._run = self._run + 1 if score >= threshold else 0
            self._best_run = max(self._best_run, self._run)
            if self._run >= self.patience:
                peak = self._peak
                self._end_episode()
                return peak
            return None
        if self._peak and self.near_miss is not None:
            self._pending = NearMiss(self._peak, self._best_run, *self.recent())
        self._end_episode()
        return None

    def recent(self) -> tuple[np.ndarray, list[float]]:
        """The last few seconds of audio fed in, and their scores."""
        if not self._history:
            return np.zeros(0, dtype=np.int16), []
        frames, scores = zip(*self._history)
        return np.concatenate(frames), [round(s, 3) for s in scores]

    def pop_near_miss(self) -> NearMiss | None:
        pending, self._pending = self._pending, None
        return pending

    def _end_episode(self) -> None:
        self._peak = 0.0
        self._run = 0
        self._best_run = 0

    def reset(self) -> None:
        self._history.clear()
        self._pending: NearMiss | None = None
        self._end_episode()


class WakeWord(Trigger):
    def __init__(self, model: str, threshold: float = 0.5, offset: float = 0.0,
                 patience: int = 1, near_miss: float | None = None):
        import openwakeword
        from openwakeword.model import Model

        # `model` is a built-in name ("hey_jarvis") or a path to a custom .onnx.
        # Either way the shared feature models must be present; fetching one
        # built-in model brings them along.
        custom = Path(model).suffix == ".onnx"
        openwakeword.utils.download_models(model_names=["hey_jarvis" if custom else model])
        self._model = Model(wakeword_models=[model], inference_framework="onnx")
        super().__init__(threshold, offset, patience, near_miss)
        self.name = Path(model).stem

    def _predict(self, frame: np.ndarray) -> float:
        return max(self._model.predict(frame).values())

    def reset(self) -> None:
        super().reset()
        self._model.reset()
