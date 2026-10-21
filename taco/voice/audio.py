"""Microphone capture and speaker playback through PipeWire.

Uses the pw-record / pw-play command-line tools rather than PortAudio, so
PipeWire handles device routing and resampling (the Wave:3 only does 48 kHz
natively, and Pi OS ships no PipeWire ALSA bridge by default). --raw is
essential: without it pw-record/pw-play wrap stdio in an .au container and
the piped stream comes through as silence.

The mic runs continuously into a queue of 80 ms int16 frames (the frame size
openWakeWord expects). Mic satellites in other rooms stream the same frames
over the LAN (NetworkMicrophone). Playback runs on its own thread so the
next sentence can be synthesized while the current one plays.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import queue
import socket
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

FRAME_SAMPLES = 1280  # 80 ms at 16 kHz
STALL_S = 3.0         # a live source this quiet has stopped streaming


def _target_args(device: str | None) -> list[str]:
    """`device` is a PipeWire node name or id (see `wpctl status`); None = default."""
    return ["--target", device] if device else []


class _FrameSource:
    """A queue of 80 ms int16 frames fed by a background thread. `ready`, if
    given, is set on every frame so one loop can wait on several sources."""

    def __init__(self, name: str, ready: threading.Event | None = None):
        self.name = name
        self.ready = ready
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.last_frame: float | None = None   # time.monotonic() of the newest frame
        self._stalled_since: float | None = None

    @property
    def live(self) -> bool:
        """Whether frames should be arriving right now."""
        return True

    def check_stall(self, now: float | None = None) -> None:
        """Log when a live source stops delivering audio, and when it comes
        back. A half-open satellite connection shows up here and nowhere else."""
        now = time.monotonic() if now is None else now
        if not self.live or self.last_frame is None:
            self._stalled_since = None
            return
        quiet = now - self.last_frame
        if self._stalled_since is None and quiet > STALL_S:
            self._stalled_since = self.last_frame
            log.warning("mic %r: no audio for %.0f s", self.name, quiet)
        elif self._stalled_since is not None and quiet <= STALL_S:
            log.info("mic %r: audio back after %.0f s", self.name, now - self._stalled_since)
            self._stalled_since = None

    def _put(self, chunk: bytes) -> None:
        self.last_frame = time.monotonic()
        try:
            self.frames.put_nowait(np.frombuffer(chunk, dtype=np.int16))
        except queue.Full:
            pass  # consumer is busy (e.g. during STT); drop rather than block
        if self.ready:
            self.ready.set()

    def read(self, timeout: float | None = None) -> np.ndarray:
        return self.frames.get(timeout=timeout)

    def read_nowait(self) -> np.ndarray | None:
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return None

    def flush(self) -> None:
        """Drop buffered audio, e.g. Taco's own voice captured while speaking."""
        while self.read_nowait() is not None:
            pass


class Microphone(_FrameSource):
    def __init__(self, sample_rate: int = 16000, device: str | None = None,
                 name: str = "kitchen", ready: threading.Event | None = None):
        super().__init__(name, ready)
        self.sample_rate = sample_rate
        self.device = device
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
                self._put(chunk)
            log.warning("pw-record exited (%s); restarting", self._proc.wait())
            threading.Event().wait(1.0)


class NetworkMicrophone(_FrameSource):
    """A mic satellite in another room (see satellite/README.md).

    The satellite connects over TCP, sends one handshake line
    `TACO <name> <token>`, then streams raw 16 kHz mono s16 audio. Only one
    connection is live at a time: a new one (the satellite restarting)
    replaces the old, which may be a half-open socket nobody will close.
    """

    sample_rate = 16000   # fixed by the satellite protocol

    def __init__(self, name: str, port: int, token: str, allow: list[str] | None = None,
                 host: str = "0.0.0.0", ready: threading.Event | None = None):
        super().__init__(name, ready)
        if not token:
            raise ValueError(f"satellite {name!r} needs a token")
        self.port = port
        self.host = host
        self._token = token
        self._allow = [ipaddress.ip_network(a, strict=False) for a in (allow or [])]
        self._lock = threading.Lock()
        self._conn: socket.socket | None = None
        self._server: socket.socket | None = None

    def start(self) -> None:
        self._server = socket.create_server((self.host, self.port))
        self.port = self._server.getsockname()[1]   # when bound to port 0 (tests)
        threading.Thread(target=self._accept, daemon=True, name=f"sat-{self.name}").start()
        log.info("satellite %r: listening on port %d", self.name, self.port)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def live(self) -> bool:
        return self.connected   # a disconnect is logged on its own

    def _allowed(self, ip: str) -> bool:
        if not self._allow:
            return True
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in net for net in self._allow)

    def _accept(self) -> None:
        assert self._server
        while True:
            conn, (ip, *_) = self._server.accept()
            threading.Thread(target=self._serve, args=(conn, ip), daemon=True,
                             name=f"sat-{self.name}-conn").start()

    def _handshake(self, conn: socket.socket) -> bool:
        conn.settimeout(5.0)
        line = b""
        while not line.endswith(b"\n") and len(line) < 256:
            byte = conn.recv(1)
            if not byte:
                return False
            line += byte
        parts = line.decode(errors="replace").split()
        return (len(parts) == 3 and parts[0] == "TACO"
                and hmac.compare_digest(parts[2].encode(), self._token.encode()))

    def _serve(self, conn: socket.socket, ip: str) -> None:
        try:
            if not self._allowed(ip):
                log.warning("satellite %r: refused %s (not in allow list)", self.name, ip)
                return
            if not self._handshake(conn):
                log.warning("satellite %r: bad handshake from %s", self.name, ip)
                return
            conn.settimeout(None)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            with self._lock:
                self.last_frame = time.monotonic()   # don't count the time it was away
                old, self._conn = self._conn, conn
            if old:
                _close(old)
            log.info("satellite %r: connected from %s", self.name, ip)
            stream = conn.makefile("rb")
            frame_bytes = FRAME_SAMPLES * 2
            while len(chunk := stream.read(frame_bytes)) == frame_bytes:
                if self._conn is not conn:
                    break   # replaced by a newer connection
                self._put(chunk)
        except OSError as e:
            log.info("satellite %r: %s", self.name, e)
        finally:
            with self._lock:
                if self._conn is conn:
                    self._conn = None
                    log.warning("satellite %r: disconnected", self.name)
            _close(conn)


def _close(conn: socket.socket) -> None:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


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
        """Cut playback short and drop anything queued (touch "stop", alarms)."""
        if self._idle.is_set():
            return  # nothing playing; a stale flag would swallow the next clip
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
