"""Microphone capture and speaker playback through PipeWire.

Uses the pw-record / pw-play command-line tools rather than PortAudio, so
PipeWire handles device routing and resampling (the Wave:3 only does 48 kHz
natively, and Pi OS ships no PipeWire ALSA bridge by default). --raw is
essential: without it pw-record/pw-play wrap stdio in an .au container and
the piped stream comes through as silence.

The mic runs continuously into a queue of 80 ms int16 frames (the frame size
openWakeWord expects). Playback runs on its own thread so the next sentence
can be synthesized while the current one plays.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading

import numpy as np

log = logging.getLogger(__name__)

FRAME_SAMPLES = 1280  # 80 ms at 16 kHz


def _target_args(device: str | None) -> list[str]:
    """`device` is a PipeWire node name or id (see `wpctl status`); None = default."""
    return ["--target", device] if device else []


class Microphone:
    def __init__(self, sample_rate: int = 16000, device: str | None = None):
        self.sample_rate = sample_rate
        self.device = device
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="mic").start()

    def _run(self) -> None:
        cmd = ["pw-record", "--raw", "--rate", str(self.sample_rate), "--channels", "1",
               "--format", "s16", *_target_args(self.device), "-"]
        frame_bytes = FRAME_SAMPLES * 2
        while True:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert self._proc.stdout
            while chunk := self._proc.stdout.read(frame_bytes):
                if len(chunk) < frame_bytes:
                    break
                try:
                    self.frames.put_nowait(np.frombuffer(chunk, dtype=np.int16))
                except queue.Full:
                    pass  # consumer is busy (e.g. during STT); drop rather than block
            log.warning("pw-record exited (%s); restarting", self._proc.wait())
            threading.Event().wait(1.0)

    def read(self, timeout: float | None = None) -> np.ndarray:
        return self.frames.get(timeout=timeout)

    def flush(self) -> None:
        """Drop buffered audio, e.g. Taco's own voice captured while speaking."""
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return


class Speaker:
    """Plays int16 mono clips sequentially on a background thread."""

    def __init__(self, device: str | None = None):
        self.device = device
        self._clips: queue.Queue[tuple[np.ndarray, int]] = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        self._stopping = threading.Event()
        self._proc: subprocess.Popen | None = None
        threading.Thread(target=self._run, daemon=True, name="speaker").start()

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self._idle.clear()
        self._clips.put((audio, sample_rate))

    def wait(self) -> None:
        self._clips.join()
        self._idle.wait()

    def stop(self) -> None:
        """Cut playback short (touch "stop", or future barge-in)."""
        self._stopping.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _run(self) -> None:
        while True:
            audio, rate = self._clips.get()
            try:
                if not self._stopping.is_set():
                    self._proc = subprocess.Popen(
                        ["pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
                         *_target_args(self.device), "-"],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    )
                    try:
                        self._proc.communicate(audio.astype(np.int16).tobytes())
                    except BrokenPipeError:
                        pass
            except Exception:
                log.exception("playback failed")
            finally:
                self._clips.task_done()
                if self._clips.empty():
                    self._stopping.clear()
                    self._idle.set()


def tone(freqs: list[float], duration: float = 0.12, rate: int = 22050, volume: float = 0.3) -> np.ndarray:
    """Short multi-note earcon, with fades to avoid clicks."""
    n = int(duration * rate)
    t = np.arange(n) / rate
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
    notes = [np.sin(2 * np.pi * f * t) * fade for f in freqs]
    return (np.concatenate(notes) * volume * 32767).astype(np.int16)
