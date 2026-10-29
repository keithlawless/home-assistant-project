"""Wake word detection (openWakeWord, ONNX backend).

The model scores every 80 ms frame. A detection needs `patience` frames in a
row at or above the threshold (the Settings sensitivity plus a per-room
offset), so a brief spike from TV audio doesn't wake Taco. A run of scores
that comes close without firing is kept as a near miss, so missed wakes can
be saved and studied (see clips.py).

An optional verifier (wakeword/verifier/) gives every candidate, meaning any
run that reaches the near-miss floor, a second score from a small model
trained on this household's voices. With `enforce` on, the verifier decides:
a candidate it accepts fires, even below the threshold, and a run it rejects
never fires, however high the wake model scored it (TV voices). Without a
verifier, or with `enforce` off, threshold and patience decide as before and
the verifier score is only logged.
"""

from __future__ import annotations

import logging
import pickle
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
    verified: float | None = None   # verifier peak; None without a verifier


class Trigger:
    """Turns per-frame scores into detections. Subclasses supply _predict."""

    def __init__(self, sensitivity: float = 0.5, offset: float = 0.0, patience: int = 1,
                 near_miss: float | None = None, verifier_threshold: float = 0.5,
                 enforce: bool = False):
        self.sensitivity = sensitivity   # from Settings
        self.offset = offset             # per room
        self.patience = max(1, patience)
        self.near_miss = near_miss       # report runs peaking at or above this; None = off
        self.verifier_threshold = verifier_threshold
        self.enforce = enforce           # the verifier, when there is one, decides
        self._history: deque[tuple[np.ndarray, float]] = deque(maxlen=HISTORY_FRAMES)
        self.reset()

    @property
    def threshold(self) -> float:
        return min(self.sensitivity + self.offset, 0.99)

    def _predict(self, frame: np.ndarray) -> float:
        raise NotImplementedError

    def _verify(self) -> float | None:
        """Verifier score for the frame just predicted; None without one."""
        return None

    def detect(self, frame: np.ndarray) -> float | None:
        """Feed one 80 ms frame; return the peak score once the wake word fires."""
        score = float(self._predict(frame))   # the model gives np.float32; clips need JSON
        self._history.append((frame, score))
        threshold = self.threshold
        floor = threshold if self.near_miss is None else min(self.near_miss, threshold)
        if score >= floor:
            if (verified := self._verify()) is not None:
                self._verified = max(self._verified or 0.0, verified)
            self._peak = max(self._peak, score)
            self._run = self._run + 1 if score >= threshold else 0
            self._best_run = max(self._best_run, self._run)
            if self._fires():
                peak = self._peak
                self.last_verified = self._verified
                self._end_episode()
                return peak
            return None
        if self._peak and self.near_miss is not None:
            self._pending = NearMiss(self._peak, self._best_run, *self.recent(), self._verified)
        self._end_episode()
        return None

    def _fires(self) -> bool:
        if self.enforce and self._verified is not None:
            return self._verified >= self.verifier_threshold
        return self._run >= self.patience

    def recent(self) -> tuple[np.ndarray, list[float]]:
        """The last few seconds of audio fed in, and their scores."""
        if not self._history:
            return np.zeros(0, dtype=np.int16), []
        frames, scores = zip(*self._history)
        return np.concatenate(frames), [round(s, 3) for s in scores]

    def level_db(self) -> float:
        """The loudest recent 80 ms frame, in dBFS (silence counts as -90)."""
        if not self._history:
            return -90.0
        rms = max(float(np.sqrt(np.mean(f.astype(np.float32) ** 2))) for f, _ in self._history)
        return max(20 * np.log10(max(rms, 1e-9) / 32768), -90.0)

    def pop_near_miss(self) -> NearMiss | None:
        pending, self._pending = self._pending, None
        return pending

    def _end_episode(self) -> None:
        self._peak = 0.0
        self._run = 0
        self._best_run = 0
        self._verified: float | None = None

    def reset(self) -> None:
        self._history.clear()
        self._pending: NearMiss | None = None
        self.last_verified: float | None = None   # verifier peak of the last detection
        self._end_episode()


class WakeWord(Trigger):
    def __init__(self, model: str, threshold: float = 0.5, offset: float = 0.0,
                 patience: int = 1, near_miss: float | None = None,
                 verifier: str | None = None, verifier_threshold: float = 0.5,
                 enforce: bool = False):
        import openwakeword
        from openwakeword.model import Model

        # `model` is a built-in name ("hey_jarvis") or a path to a custom .onnx.
        # Either way the shared feature models must be present; fetching one
        # built-in model brings them along.
        custom = Path(model).suffix == ".onnx"
        openwakeword.utils.download_models(model_names=["hey_jarvis" if custom else model])
        self._model = Model(wakeword_models=[model], inference_framework="onnx")
        self._key = next(iter(self._model.models))
        self._verifier = load_verifier(verifier, enforce) if verifier else None
        super().__init__(threshold, offset, patience, near_miss, verifier_threshold, enforce)
        self.name = Path(model).stem

    def _predict(self, frame: np.ndarray) -> float:
        return max(self._model.predict(frame).values())

    def _verify(self) -> float | None:
        if self._verifier is None:
            return None
        # The same features the wake model just scored (openWakeWord's own
        # custom_verifier_models hook does this, but it replaces the score).
        features = self._model.preprocessor.get_features(self._model.model_inputs[self._key])
        return float(self._verifier.predict_proba(features)[0][-1])

    def verdict(self, verified: float | None) -> str:
        """Log text for a verifier score: what it decided, or would have."""
        if verified is None:
            return ""
        return f"verifier {verified:.2f} {'accept' if verified >= self.verifier_threshold else 'reject'}"

    def reset(self) -> None:
        super().reset()
        self._model.reset()


def load_verifier(path: str, enforce: bool = False):
    """The scikit-learn pipeline written by wakeword/verifier/verifier.py, or
    None if it isn't there (the wake word then works as before)."""
    try:
        with open(path, "rb") as f:
            model = pickle.load(f)
    except FileNotFoundError:
        log.warning("wake verifier %s not found; running without it", path)
        return None
    log.info("wake verifier %s loaded (%s)", path, "enforcing" if enforce else "log only")
    return model


def verifier_path(model: str, setting: str) -> str | None:
    """[wake] verifier: "auto" finds <model>_verifier.pkl beside a custom model."""
    if setting != "auto":
        return setting or None
    path = Path(model).with_name(f"{Path(model).stem}_verifier.pkl")
    return str(path) if Path(model).suffix == ".onnx" and path.exists() else None
