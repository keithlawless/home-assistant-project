"""Local speech-to-text with faster-whisper."""

from __future__ import annotations

import logging

import numpy as np

from taco.config import TACO_HOME

log = logging.getLogger(__name__)

# Whisper invents these on near-silent audio.
_HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "bye.", "."}


class Transcriber:
    def __init__(self, model: str = "base.en", compute_type: str = "int8"):
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            model, device="cpu", compute_type=compute_type, cpu_threads=4,
            download_root=str(TACO_HOME / "models" / "whisper"),
        )
        log.info("whisper ready: %s", model)

    def transcribe(self, audio: np.ndarray) -> str:
        samples = audio.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            samples, language="en", beam_size=1, vad_filter=False, without_timestamps=True,
            initial_prompt="Hey Taco.",
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return "" if text.lower() in _HALLUCINATIONS else text
