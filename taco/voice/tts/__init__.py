"""Text-to-speech engines behind one interface.

Each engine turns one sentence into (int16 mono audio, sample rate).
Piper is fast; Kokoro is more natural but about real time on a Pi 5.
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
    if engine == "kokoro":
        from .kokoro import KokoroEngine
        return KokoroEngine(voice)
    raise ValueError(f"unknown TTS engine: {engine}")


class EngineCache:
    """Loaded engines by voice id, so switching back is instant."""

    def __init__(self):
        self._engines: dict[str, TTSEngine] = {}

    def get(self, voice_id: str) -> TTSEngine:
        if voice_id not in self._engines:
            engine, voice = voice_id.split(":", 1)
            self._engines[voice_id] = create_engine(engine, voice)
        return self._engines[voice_id]
