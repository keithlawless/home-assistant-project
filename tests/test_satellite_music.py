import sys
import threading
import time

import numpy as np

from taco.voice.music import CHUNK_S, CUSHION_S, RATE, SatelliteMusic


class FakeSat:
    def __init__(self, name, rate=48000):
        self.name, self.speaker_rate = name, rate
        self.connected = self.speaker = True
        self.audio, self.lines = [], []

    def send_audio(self, pcm, kind=b"AUDIO"):
        self.audio.append((kind, len(pcm)))
        return True

    def send_line(self, line):
        self.lines.append(line)


def _source(seconds_music: float, seconds_silence: float) -> list[str]:
    """A stand-in for pw-record: music (a tone), then silence, then it waits."""
    n_music, n_quiet = int(RATE * seconds_music), int(RATE * seconds_silence)
    code = (
        "import sys, time, numpy as np\n"
        f"t = np.arange({n_music}) / {RATE}\n"
        "sys.stdout.buffer.write((np.sin(t * 2000) * 8000).astype(np.int16).tobytes())\n"
        f"sys.stdout.buffer.write(np.zeros({n_quiet}, dtype=np.int16).tobytes())\n"
        "sys.stdout.flush(); time.sleep(30)\n")
    return [sys.executable, "-c", code]


def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_feeds_routed_satellites_with_a_cushion_and_stops_on_silence():
    bed, living = FakeSat("bedroom"), FakeSat("living room", rate=16000)
    music = SatelliteMusic(lambda: [bed, living], capture=_source(1.0, 2.0))
    music.set_rooms(["bedroom", "living room"])
    chunks = int(1.0 / CHUNK_S) + int(1.0 / CHUNK_S)   # the music, then silence until it gives up
    assert _until(lambda: len(bed.audio) >= chunks + 1)
    time.sleep(0.3)
    assert bed.audio[0] == (b"MUSIC", int(RATE * CUSHION_S) * 2)        # the cushion first
    assert all(kind == b"MUSIC" for kind, _ in bed.audio)
    assert len(bed.audio) == chunks + 1                                  # nothing after 1 s of silence
    assert living.audio[1] == (b"MUSIC", int(16000 * CHUNK_S) * 2)       # resampled for 16 kHz firmware
    music.set_rooms(["living room"])
    assert bed.lines == ["MSTOP"] and living.lines == []
    music.set_rooms([])


def test_no_capture_without_satellites():
    started = threading.Event()
    music = SatelliteMusic(lambda: [], capture=[sys.executable, "-c", "import time; time.sleep(5)"])
    music._wake.set()   # nothing routed: nothing starts
    time.sleep(0.2)
    assert music._proc is None and not started.is_set()
