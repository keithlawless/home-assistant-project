"""Piper TTS: fast, local, many voices."""

from __future__ import annotations

import logging
import subprocess
import sys

import numpy as np

from taco.config import TACO_HOME

log = logging.getLogger(__name__)

VOICES_DIR = TACO_HOME / "voices" / "piper"


def ensure_voice(voice: str) -> None:
    if (VOICES_DIR / f"{voice}.onnx").exists():
        return
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    log.info("downloading piper voice %s", voice)
    subprocess.run(
        [sys.executable, "-m", "piper.download_voices", voice, "--data-dir", str(VOICES_DIR)],
        check=True,
    )


class PiperEngine:
    def __init__(self, voice: str):
        from piper import PiperVoice

        ensure_voice(voice)
        self.voice = voice
        self._voice = PiperVoice.load(str(VOICES_DIR / f"{voice}.onnx"))
        self.sample_rate = self._voice.config.sample_rate
        log.info("piper ready: %s @ %d Hz", voice, self.sample_rate)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = [c.audio_int16_array for c in self._voice.synthesize(text)]
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
        return audio, self.sample_rate
