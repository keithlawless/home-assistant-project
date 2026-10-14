"""Capture one spoken request: wait for speech to start, stop on silence."""

from __future__ import annotations

import queue
import time

import numpy as np
import webrtcvad

from .audio import Microphone

_VAD_FRAME = 320  # 20 ms at 16 kHz; each 80 ms mic frame is 4 VAD frames


def record_request(mic: Microphone, *, start_timeout_s: float, end_silence_s: float,
                   max_seconds: float, aggressiveness: int = 2) -> np.ndarray | None:
    """Return int16 audio of the utterance, or None if nobody spoke."""
    vad = webrtcvad.Vad(aggressiveness)
    rate = mic.sample_rate
    frames: list[np.ndarray] = []
    speech_started = False
    speech_run = 0          # consecutive voiced VAD frames (debounce clicks)
    silence = 0.0
    began = time.monotonic()

    while True:
        try:
            frame = mic.read(timeout=1.0)
        except queue.Empty:
            # A satellite can stop streaming mid-request (Wi-Fi drop, reboot);
            # still honour the timeouts rather than wait for it forever.
            elapsed = time.monotonic() - began
            if not speech_started and elapsed > start_timeout_s:
                return None
            if speech_started and elapsed >= max_seconds:
                return np.concatenate(frames)
            continue
        frames.append(frame)
        voiced = sum(
            vad.is_speech(frame[i:i + _VAD_FRAME].tobytes(), rate)
            for i in range(0, len(frame), _VAD_FRAME)
        )
        frame_s = len(frame) / rate
        elapsed = time.monotonic() - began

        if voiced >= 2:
            speech_run += 1
            silence = 0.0
            if speech_run >= 2:
                speech_started = True
        else:
            speech_run = 0
            silence += frame_s

        if not speech_started:
            if elapsed > start_timeout_s:
                return None
            # Keep ~0.5 s of pre-roll so the first word isn't clipped.
            frames = frames[-6:]
            continue
        if silence >= end_silence_s or elapsed >= max_seconds:
            return np.concatenate(frames)
