"""Text-to-speech engines behind one interface.

Each engine turns one sentence into (int16 mono audio, sample rate).
Phase 5 adds Kokoro and cloud engines alongside Piper.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class TTSEngine(Protocol):
    voice: str

    def synthesize(self, text: str) -> tuple[np.ndarray, int]: ...


def create_engine(engine: str, voice: str) -> TTSEngine:
    if engine == "piper":
        from .piper import PiperEngine
        return PiperEngine(voice)
    raise ValueError(f"unknown TTS engine: {engine}")
