"""Kokoro TTS (kokoro-onnx, int8): the most natural local voices.

About real time on a Pi 5, so it adds a second or two before speech starts;
later sentences are synthesized while earlier ones play. One model instance
serves every Kokoro voice.
"""

from __future__ import annotations

import logging
import threading
import urllib.request

import numpy as np

from taco.config import TACO_HOME

log = logging.getLogger(__name__)

MODEL_DIR = TACO_HOME / "voices" / "kokoro"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")

_model = None
_model_lock = threading.Lock()


def _load():
    global _model
    with _model_lock:
        if _model is None:
            from kokoro_onnx import Kokoro

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            for name in FILES:
                path = MODEL_DIR / name
                if not path.exists():
                    log.info("downloading %s", name)
                    urllib.request.urlretrieve(f"{RELEASE}/{name}", path)
            _model = Kokoro(str(MODEL_DIR / FILES[0]), str(MODEL_DIR / FILES[1]))
    return _model


class KokoroEngine:
    def __init__(self, voice: str):
        self.voice = voice
        self._model = _load()
        self._lang = "en-gb" if voice.startswith("b") else "en-us"
        log.info("kokoro ready: %s", voice)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        samples, rate = self._model.create(text, voice=self.voice, speed=1.0, lang=self._lang)
        audio = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        return audio, rate
