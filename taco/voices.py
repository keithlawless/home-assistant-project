"""The voices Taco offers, shared by taco-core (settings, picker) and
taco-voice (synthesis). Ids are "engine:voice"."""

from __future__ import annotations

DEFAULT_VOICE = "piper:en_US-lessac-medium"

VOICES: list[dict[str, str]] = [
    # Piper: fast (speech starts almost immediately).
    {"id": "piper:en_US-lessac-medium", "name": "Lessac", "accent": "American", "gender": "female",
     "engine": "piper", "note": "Warm and clear"},
    {"id": "piper:en_US-amy-medium", "name": "Amy", "accent": "American", "gender": "female",
     "engine": "piper", "note": "Bright"},
    {"id": "piper:en_US-ryan-high", "name": "Ryan", "accent": "American", "gender": "male",
     "engine": "piper", "note": "Crisp"},
    {"id": "piper:en_US-hfc_male-medium", "name": "Hal", "accent": "American", "gender": "male",
     "engine": "piper", "note": "Relaxed"},
    {"id": "piper:en_GB-alba-medium", "name": "Alba", "accent": "Scottish", "gender": "female",
     "engine": "piper", "note": "Gentle"},
    {"id": "piper:en_GB-northern_english_male-medium", "name": "Northern", "accent": "English",
     "gender": "male", "engine": "piper", "note": "Friendly"},
    # Kokoro: the most natural, but adds a second or two before speech starts on the Pi.
    {"id": "kokoro:af_heart", "name": "Heart", "accent": "American", "gender": "female",
     "engine": "kokoro", "note": "Most natural"},
    {"id": "kokoro:af_bella", "name": "Bella", "accent": "American", "gender": "female",
     "engine": "kokoro", "note": "Expressive"},
    {"id": "kokoro:am_michael", "name": "Michael", "accent": "American", "gender": "male",
     "engine": "kokoro", "note": "Smooth"},
    {"id": "kokoro:am_fenrir", "name": "Fenrir", "accent": "American", "gender": "male",
     "engine": "kokoro", "note": "Deep"},
    {"id": "kokoro:bf_emma", "name": "Emma", "accent": "British", "gender": "female",
     "engine": "kokoro", "note": "Polished"},
    {"id": "kokoro:bm_george", "name": "George", "accent": "British", "gender": "male",
     "engine": "kokoro", "note": "Classic"},
]

PREVIEW_TEXT = "Hi, I'm Taco. This is how I'll sound when I answer you."


def split_id(voice_id: str) -> tuple[str, str]:
    engine, _, voice = voice_id.partition(":")
    return engine, voice


def is_known(voice_id: str) -> bool:
    return any(v["id"] == voice_id for v in VOICES)
