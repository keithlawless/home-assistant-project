"""Local speech-to-text with faster-whisper."""

from __future__ import annotations

import logging

import numpy as np

from taco.config import TACO_HOME

log = logging.getLogger(__name__)

# Whisper invents these on near-silent audio.
_HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "bye.", "."}

# Whisper's prompt holds 224 tokens; a short word list leaves plenty of room.
MAX_VOCABULARY = 40


def build_vocabulary(location: dict | None, extra: list[str]) -> list[str]:
    """Words to bias recognition toward: the home town (the part of the saved
    location's name before the comma) plus [stt] vocabulary from config.toml.
    Whisper spells local names it hasn't seen phonetically ("North Poro")."""
    words = []
    if location and location.get("name"):
        words.append(location["name"].split(",")[0])
    words += extra
    seen: set[str] = set()
    out = []
    for w in (w.strip() for w in words):
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out[:MAX_VOCABULARY]


class Transcriber:
    def __init__(self, model: str = "base.en", compute_type: str = "int8",
                 vocabulary: list[str] | None = None):
        from faster_whisper import WhisperModel

        self.vocabulary = vocabulary or []
        self._model = WhisperModel(
            model, device="cpu", compute_type=compute_type, cpu_threads=4,
            download_root=str(TACO_HOME / "models" / "whisper"),
        )
        log.info("whisper ready: %s", model)

    def set_vocabulary(self, words: list[str]) -> None:
        if words != self.vocabulary:
            self.vocabulary = words
            log.info("stt vocabulary: %s", ", ".join(words) or "(none)")

    def transcribe(self, audio: np.ndarray) -> str:
        samples = audio.astype(np.float32) / 32768.0
        words = ", ".join(self.vocabulary)
        segments, _ = self._model.transcribe(
            samples, language="en", beam_size=1, vad_filter=False, without_timestamps=True,
            initial_prompt=f"Hey Taco. {words}." if words else "Hey Taco.",
            hotwords=words or None,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return "" if text.lower() in _HALLUCINATIONS else text
