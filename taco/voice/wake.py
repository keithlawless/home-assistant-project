"""Wake word detection (openWakeWord, ONNX backend)."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class WakeWord:
    def __init__(self, model: str, threshold: float = 0.5):
        import openwakeword
        from openwakeword.model import Model

        # `model` is a built-in name ("hey_jarvis") or a path to a custom .onnx.
        # Either way the shared feature models must be present; fetching one
        # built-in model brings them along.
        custom = Path(model).suffix == ".onnx"
        openwakeword.utils.download_models(model_names=["hey_jarvis" if custom else model])
        self._model = Model(wakeword_models=[model], inference_framework="onnx")
        self.threshold = threshold
        self.name = Path(model).stem
        log.info("wake word ready: %s (threshold %.2f)", self.name, threshold)

    def detect(self, frame: np.ndarray) -> float | None:
        """Feed one 80 ms frame; return the score if it crosses the threshold."""
        scores = self._model.predict(frame)
        score = max(scores.values())
        return score if score >= self.threshold else None

    def reset(self) -> None:
        self._model.reset()
