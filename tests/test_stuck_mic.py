import logging

import numpy as np

from taco.voice import audio
from taco.voice.audio import FRAME_SAMPLES, _FrameSource

HUM = (np.sin(np.arange(FRAME_SAMPLES) * 2 * np.pi * 124 / 16000) * 9000).astype(np.int16)  # about -14 dBFS
QUIET = np.full(FRAME_SAMPLES, 30, dtype=np.int16)                                         # about -61 dBFS


def feed(src, frame, seconds, clock, monkeypatch):
    """Feed `seconds` of 80 ms frames, moving the fake clock along."""
    for _ in range(int(seconds / 0.08)):
        clock[0] += 0.08
        monkeypatch.setattr(audio.time, "monotonic", lambda: clock[0])
        src._put(frame.tobytes())
        src.flush()


def test_steady_loud_noise_is_stuck_and_quiet_frame_clears_it(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="taco.voice.audio")
    src, clock = _FrameSource("kitchen"), [0.0]
    assert src.check_stuck() is None                 # no audio yet
    feed(src, HUM, 59, clock, monkeypatch)
    assert src.check_stuck() is None and not src.stuck
    feed(src, HUM, 2, clock, monkeypatch)
    assert src.check_stuck() is True and src.stuck
    assert "stuck" in caplog.records[-1].message
    assert src.check_stuck() is None                 # reported once
    feed(src, QUIET, 0.08, clock, monkeypatch)
    assert src.check_stuck() is False and not src.stuck


def test_speech_with_gaps_is_not_stuck(monkeypatch):
    src, clock = _FrameSource("kitchen"), [0.0]
    for _ in range(30):                               # 2.4 s loud, 0.16 s pause, for 77 s
        feed(src, HUM, 2.4, clock, monkeypatch)
        feed(src, QUIET, 0.16, clock, monkeypatch)
    assert src.check_stuck() is None and not src.stuck


def test_silence_is_not_stuck(monkeypatch):
    src, clock = _FrameSource("bedroom"), [0.0]
    feed(src, np.zeros(FRAME_SAMPLES, np.int16), 120, clock, monkeypatch)   # e.g. a muted satellite
    assert src.check_stuck() is None
