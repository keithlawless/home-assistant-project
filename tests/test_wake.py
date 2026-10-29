import json
import logging

import numpy as np

from taco.voice.audio import FRAME_SAMPLES, Microphone
from taco.voice.clips import ClipRecorder
from taco.voice.wake import HISTORY_FRAMES, Trigger, verifier_path

FRAME = np.zeros(FRAME_SAMPLES, dtype=np.int16)


class Scripted(Trigger):
    """A Trigger fed a list of scores instead of a model, and optionally a
    list of verifier scores, one per frame."""

    def __init__(self, scores, verified=None, **kw):
        self.scores = iter(scores)
        self.verified = iter(verified) if verified is not None else None
        super().__init__(**kw)

    def _predict(self, frame):
        self._v = next(self.verified) if self.verified is not None else None
        return next(self.scores)

    def _verify(self):
        return self._v

    def run(self, n):
        return [self.detect(FRAME) for _ in range(n)]


def test_fires_on_first_frame_over_threshold_by_default():
    t = Scripted([0.1, 0.6, 0.2])
    assert t.run(3) == [None, 0.6, None]


def test_scores_are_plain_floats(tmp_path):
    t = Scripted([np.float32(0.7)], near_miss=0.3)
    score = t.detect(FRAME)
    assert type(score) is float and type(t.recent()[1][0]) is float


def test_patience_needs_consecutive_frames():
    t = Scripted([0.9, 0.1, 0.6, 0.7, 0.8], patience=3)
    assert t.run(5) == [None, None, None, None, 0.8]


def test_peak_is_reported_and_offset_raises_threshold():
    t = Scripted([0.55, 0.9, 0.62], offset=0.1, patience=2)
    assert t.threshold == 0.6
    assert t.run(3) == [None, None, 0.9]   # 0.55 is below 0.6, so the run starts at 0.9


def test_threshold_is_capped():
    assert Scripted([], sensitivity=0.7, offset=0.5).threshold == 0.99


def test_near_miss_reported_when_run_ends():
    t = Scripted([0.1, 0.35, 0.45, 0.2], near_miss=0.3)
    t.run(3)
    assert t.pop_near_miss() is None   # still in progress
    t.run(1)
    miss = t.pop_near_miss()
    assert miss.peak == 0.45 and miss.frames == 0
    assert miss.scores == [0.1, 0.35, 0.45, 0.2]
    assert len(miss.audio) == 4 * FRAME_SAMPLES
    assert t.pop_near_miss() is None


def test_debounced_spike_is_a_near_miss():
    t = Scripted([0.9, 0.1], patience=2, near_miss=0.3)
    assert t.run(2) == [None, None]
    miss = t.pop_near_miss()
    assert miss.peak == 0.9 and miss.frames == 1


def test_no_near_miss_after_firing_or_when_off():
    t = Scripted([0.6, 0.1], near_miss=0.3)
    assert t.run(2) == [0.6, None]
    assert t.pop_near_miss() is None
    t = Scripted([0.4, 0.1])
    t.run(2)
    assert t.pop_near_miss() is None


def test_verifier_peak_kept_for_detection():
    t = Scripted([0.2, 0.4, 0.6, 0.7], verified=[0.99, 0.3, 0.8, 0.5], patience=2, near_miss=0.3)
    assert t.run(4) == [None, None, None, 0.7]
    assert t.last_verified == 0.8   # 0.99 came with a frame below the floor


def test_verifier_peak_kept_for_near_miss():
    t = Scripted([0.9, 0.1], verified=[0.2, 0.0], patience=2, near_miss=0.3)
    t.run(2)
    assert t.pop_near_miss().verified == 0.2


def test_no_verifier_means_none():
    t = Scripted([0.9, 0.1, 0.6], patience=1, near_miss=0.3)
    t.run(1)
    assert t.last_verified is None


def test_verifier_path(tmp_path):
    model = tmp_path / "hey_taco.onnx"
    assert verifier_path(str(model), "auto") is None
    (tmp_path / "hey_taco_verifier.pkl").write_bytes(b"")
    assert verifier_path(str(model), "auto") == str(tmp_path / "hey_taco_verifier.pkl")
    assert verifier_path("hey_jarvis", "auto") is None
    assert verifier_path(str(model), "") is None
    assert verifier_path(str(model), "/x.pkl") == "/x.pkl"


def test_history_is_bounded_and_reset_clears():
    t = Scripted([0.0] * (HISTORY_FRAMES + 5))
    t.run(HISTORY_FRAMES + 5)
    audio, scores = t.recent()
    assert len(scores) == HISTORY_FRAMES and len(audio) == HISTORY_FRAMES * FRAME_SAMPLES
    t.reset()
    assert t.recent()[1] == []


def test_clips_written_and_pruned(tmp_path):
    clips = ClipRecorder(tmp_path, max_clips=2)
    for i in range(3):
        clips.save("wake", "Living Room", FRAME, {"score": i})
        clips.wait()
    wavs = sorted((tmp_path / "wake").glob("*.wav"))
    assert len(wavs) == 2 and "_living-room" in wavs[0].name
    meta = json.loads(wavs[-1].with_suffix(".json").read_text())
    assert meta["score"] == 2 and meta["room"] == "Living Room" and meta["kind"] == "wake"


def test_near_miss_clips_rate_limited_per_room(tmp_path):
    clips = ClipRecorder(tmp_path, near_miss_gap_s=60)
    for room in ("kitchen", "kitchen", "living room"):
        clips.save("near_miss", room, FRAME, {})
    clips.wait()
    assert len(list((tmp_path / "near_miss").glob("*.wav"))) == 2


def test_stall_logged_once_and_recovery(caplog):
    caplog.set_level(logging.INFO, logger="taco.voice.audio")
    mic = Microphone()
    mic.check_stall(now=100.0)            # nothing yet: no warning
    mic._put(FRAME.tobytes())
    t0 = mic.last_frame
    mic.check_stall(now=t0 + 1)
    mic.check_stall(now=t0 + 5)
    mic.check_stall(now=t0 + 9)
    assert [r.message for r in caplog.records] == ["mic 'kitchen': no audio for 5 s"]
    mic.last_frame = t0 + 20
    mic.check_stall(now=t0 + 20)
    assert caplog.records[-1].message == "mic 'kitchen': audio back after 20 s"


def test_level_db_is_loudest_recent_frame():
    t = Scripted([0.0, 0.0])
    assert t.level_db() == -90.0
    t.detect(FRAME)
    assert t.level_db() == -90.0   # digital silence
    t.detect(np.full(FRAME_SAMPLES, 3277, dtype=np.int16))
    assert abs(t.level_db() - -20.0) < 0.1
    t.reset()
    assert t.level_db() == -90.0


def _loop_with(local_level, satellite_level, limit=10.0):
    from taco.voice.audio import NetworkMicrophone
    from taco.voice.main import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)   # no models or devices
    loop.cfg = {"wake": {"local_priority_db": limit}}
    loop.clips = None
    kitchen, living = Scripted([0.0]), Scripted([0.9])
    kitchen.detect(np.full(FRAME_SAMPLES, local_level, dtype=np.int16))
    living.detect(np.full(FRAME_SAMPLES, satellite_level, dtype=np.int16))
    satellite = NetworkMicrophone.__new__(NetworkMicrophone)
    satellite.name = "living room"
    loop.listeners = [(Microphone(), kitchen), (satellite, living)]
    return loop, satellite, living


def test_satellite_wake_ignored_when_kitchen_much_louder(caplog):
    caplog.set_level(logging.INFO, logger="taco.voice")
    loop, satellite, living = _loop_with(4000, 300)   # ~22 dB
    assert loop._heard_locally(satellite, living, 0.9)
    assert "ignored: kitchen heard it 22 dB louder" in caplog.records[-1].message
    assert living.level_db() == -90.0   # reset


def test_satellite_wake_kept_when_speech_is_in_its_room():
    loop, satellite, living = _loop_with(300, 300)
    assert not loop._heard_locally(satellite, living, 0.9)
    loop, satellite, living = _loop_with(4000, 300, limit=0)   # off
    assert not loop._heard_locally(satellite, living, 0.9)


def test_local_mic_never_ignored():
    loop, _, _ = _loop_with(4000, 300)
    mic, wake = loop.listeners[0]
    assert not loop._heard_locally(mic, wake, 0.9)
