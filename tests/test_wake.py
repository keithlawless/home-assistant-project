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


class FakeMic:
    """Hands out queued frames, like Microphone.read_nowait."""

    def __init__(self, name, frames=()):
        self.name = name
        self.frames = list(frames)

    def read_nowait(self):
        return self.frames.pop(0) if self.frames else None


def _loop_with(local_level, satellite_level, limit=10.0, kitchen_scores=(0.0,)):
    from taco.voice.main import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)   # no models or devices
    loop.cfg = {"wake": {"local_priority_db": limit, "local_defer_s": 0.8, "local_listen_db": 15.0,
                        "afe_local_priority_db": 36.0, "afe_local_listen_db": 42.0,
                        "afe_faint_db": -48.0}}
    loop.clips = None
    loop.muted = False
    loop._deferred = None
    kitchen = Scripted(list(kitchen_scores) + [0.0] * 20)
    living = Scripted([0.9] + [0.0] * 20)
    mic = FakeMic("kitchen", [np.full(FRAME_SAMPLES, local_level, dtype=np.int16)])
    satellite = FakeMic("living room", [np.full(FRAME_SAMPLES, satellite_level, dtype=np.int16)])
    loop.listeners = [(mic, kitchen), (satellite, living)]
    return loop, mic, satellite


def test_satellite_wake_waits_then_fires_when_kitchen_stays_quiet(caplog):
    caplog.set_level(logging.INFO, logger="taco.voice")
    loop, mic, satellite = _loop_with(1200, 300)   # ~12 dB: living-room speech
    assert loop._detect_wake(now=0.0) is None
    assert "waits: kitchen heard it 12 dB louder" in caplog.records[-1].message
    satellite.frames.append(FRAME)   # request audio stays buffered meanwhile
    mic.frames.append(FRAME)
    assert loop._detect_wake(now=0.5) is None
    assert satellite.frames == [FRAME]
    score, detector, listener = loop._detect_wake(now=0.9)
    assert score == 0.9 and detector[0] is satellite and listener[0] is satellite
    assert loop._deferred is None


def test_kitchen_wins_when_it_wakes_too(caplog):
    caplog.set_level(logging.INFO, logger="taco.voice")
    loop, mic, satellite = _loop_with(4000, 300, kitchen_scores=(0.0, 0.8))   # ~22 dB
    assert loop._detect_wake(now=0.0) is None
    mic.frames.append(FRAME)
    score, detector, listener = loop._detect_wake(now=0.3)
    assert score == 0.8 and detector[0] is mic and listener[0] is mic
    assert "ignored: kitchen heard it 22 dB louder" in caplog.records[-1].message
    assert loop._deferred is None


def test_fallback_listens_on_kitchen_when_far_louder():
    loop, mic, satellite = _loop_with(3200, 300)   # ~21 dB: the speaker is in the kitchen
    assert loop._detect_wake(now=0.0) is None
    score, detector, listener = loop._detect_wake(now=1.0)
    assert detector[0] is satellite and listener[0] is mic


def test_satellite_wake_fires_at_once_when_speech_is_in_its_room():
    loop, mic, satellite = _loop_with(300, 300)
    assert loop._detect_wake(now=0.0)[1][0] is satellite
    loop, mic, satellite = _loop_with(4000, 300, limit=0)   # off
    assert loop._detect_wake(now=0.0)[1][0] is satellite


def test_local_mic_never_waits():
    loop, mic, satellite = _loop_with(4000, 300, kitchen_scores=(0.8,))
    satellite.frames.clear()
    assert loop._detect_wake(now=0.0)[1][0] is mic
    assert loop._deferred is None


def test_enforced_verifier_rescues_low_scoring_wake():
    t = Scripted([0.1, 0.35, 0.4, 0.1], verified=[None, 0.3, 0.9, None], patience=2,
                 near_miss=0.3, enforce=True)
    assert t.run(4) == [None, None, 0.4, None]   # fires on the accepting frame
    assert t.last_verified == 0.9 and t.pop_near_miss() is None


def test_enforced_verifier_rejects_high_scoring_run():
    t = Scripted([0.9, 0.95, 0.92, 0.1], verified=[0.05, 0.08, 0.04, None], patience=2,
                 near_miss=0.3, enforce=True)
    assert t.run(4) == [None] * 4
    miss = t.pop_near_miss()
    assert miss.peak == 0.95 and miss.frames == 3 and miss.verified == 0.08


def test_log_only_verifier_leaves_threshold_in_charge():
    t = Scripted([0.4, 0.9, 0.95], verified=[0.99, 0.01, 0.01], patience=2, near_miss=0.3)
    assert t.run(3) == [None, None, 0.95]


def test_enforce_without_verifier_falls_back_to_threshold():
    t = Scripted([0.4, 0.9, 0.95], patience=2, near_miss=0.3, enforce=True)
    assert t.run(3) == [None, None, 0.95]


def test_satellite_channels_waiting_together_keep_the_stronger(caplog):
    """Both channels of a satellite's front end can hear the same words; the
    one that scored higher is the one that waits (and may fire)."""
    from taco.voice.main import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)
    loop.cfg = {"wake": {"local_priority_db": 10.0, "local_defer_s": 0.8, "local_listen_db": 15.0,
                        "afe_local_priority_db": 36.0, "afe_local_listen_db": 42.0,
                        "afe_faint_db": -48.0}}
    loop.clips = None
    loop.muted = False
    loop._deferred = None
    kitchen = Scripted([0.0] * 20)
    ch1, ch2 = Scripted([0.6] + [0.0] * 20), Scripted([0.9] + [0.0] * 20)
    mic = FakeMic("kitchen", [np.full(FRAME_SAMPLES, 1200, dtype=np.int16)])
    sat1 = FakeMic("living room", [np.full(FRAME_SAMPLES, 300, dtype=np.int16)])
    sat2 = FakeMic("living room 2", [np.full(FRAME_SAMPLES, 300, dtype=np.int16)])
    loop.listeners = [(mic, kitchen), (sat1, ch1), (sat2, ch2)]
    assert loop._detect_wake(now=0.0) is None
    assert loop._deferred["listener"][0] is sat2 and loop._deferred["score"] == 0.9
    score, detector, listener = loop._detect_wake(now=1.0)
    assert score == 0.9 and detector[0] is sat2 and listener[0] is sat2


def test_front_end_satellite_uses_its_own_margins():
    """With the speech front end on, couch speech reads +19..+27 dB: it fires
    at once on the satellite instead of going to the kitchen."""
    loop, mic, satellite = _loop_with(6000, 300)   # ~26 dB
    satellite.afe = "on"
    score, detector, listener = loop._detect_wake(now=0.0)
    assert detector[0] is satellite and listener[0] is satellite and loop._deferred is None


def test_front_end_satellite_still_defers_far_louder_kitchen_speech():
    loop, mic, satellite = _loop_with(24000, 300)  # ~38 dB: someone at the kitchen mic
    satellite.afe = "on"
    assert loop._detect_wake(now=0.0) is None      # waits for the kitchen
    score, detector, listener = loop._detect_wake(now=1.0)
    assert listener[0] is satellite                # 38 < 42: the satellite keeps it


def test_front_end_satellite_fires_at_once_for_loud_couch_speech():
    loop, mic, satellite = _loop_with(10500, 300)  # ~31 dB, the loudest couch reading
    satellite.afe = "on"
    assert loop._detect_wake(now=0.0)[2][0] is satellite


def test_front_end_satellite_defers_kitchen_speech_it_heard_faintly():
    """With music on, kitchen speech reached the satellite at a couch-like
    margin (+31 dB) but faintly (-52 dBFS): it waits, and if the kitchen
    doesn't wake, the request still comes from the kitchen mic."""
    loop, mic, satellite = _loop_with(2900, 82)    # ~31 dB margin; satellite ~ -52 dBFS
    satellite.afe = "on"
    assert loop._detect_wake(now=0.0) is None      # waits for the kitchen
    score, detector, listener = loop._detect_wake(now=1.0)
    assert detector[0] is satellite and listener[0] is mic


def test_front_end_couch_speech_is_not_faint():
    loop, mic, satellite = _loop_with(2900, 600)   # ~14 dB margin; satellite ~ -35 dBFS
    satellite.afe = "on"
    assert loop._detect_wake(now=0.0)[2][0] is satellite


def test_front_end_faint_wake_stays_put_when_the_kitchen_heard_less():
    """Quiet bedroom speech reads -49..-69 dBFS on the satellite, but the
    kitchen hears it 12-40 dB quieter still: the satellite keeps the turn
    instead of handing the request to a kitchen mic that can't hear it."""
    loop, mic, satellite = _loop_with(20, 82)      # ~-12 dB margin; satellite ~ -52 dBFS
    satellite.afe = "on"
    score, detector, listener = loop._detect_wake(now=0.0)
    assert detector[0] is satellite and listener[0] is satellite and loop._deferred is None
