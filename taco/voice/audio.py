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

import hashlib
import hmac
import ipaddress
import logging
import queue
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

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


class SatelliteChannel(_FrameSource):
    """The second channel of a satellite that streams two (its speech front
    end's separated sources). It gets its own wake model, like any mic, so
    whichever channel holds the speaker's voice wins the wake."""

    sample_rate = 16000   # fixed by the satellite protocol, as for NetworkMicrophone

    def __init__(self, parent: "NetworkMicrophone", ready: threading.Event | None = None):
        super().__init__(f"{parent.name} 2", ready)
        self.parent = parent

    def start(self) -> None:
        """Nothing to start: the parent's connection feeds it."""

    @property
    def live(self) -> bool:
        return self.parent.connected and self.parent.channels == 2


class SatelliteMix:
    """Both channels of a two-channel satellite, added together: what a
    request is recorded from. The front end can move the speaker to the
    other channel mid-turn (for one, once the TV pauses), so the request
    mustn't depend on which channel heard the wake word. Not a wake listener:
    while a turn is on, the voice loop isn't reading the channels itself."""

    sample_rate = 16000

    def __init__(self, sat: "NetworkMicrophone"):
        self.sat = sat
        self.name = f"{sat.name} (both channels)"

    def read(self, timeout: float | None = None) -> np.ndarray:
        a = self.sat.read(timeout=timeout)
        try:
            b = self.sat.second.read(timeout=0.2)   # fed with a, frame for frame
        except queue.Empty:
            return a
        return np.clip(a.astype(np.int32) + b, -32768, 32767).astype(np.int16)

    def flush(self) -> None:
        self.sat.flush()
        self.sat.second.flush()


class NetworkMicrophone(_FrameSource):
    """A mic satellite in another room (see satellite/README.md).

    The satellite connects over TCP, sends one handshake line
    `TACO <name> <token> [afe=on|off] [ch=1|2]`, then streams raw 16 kHz s16
    audio: mono, or with ch=2 two interleaved channels, the second of which
    goes to `self.second`. Only one connection is live at a time: a new one
    (the satellite restarting) replaces the old, which may be a half-open
    socket nobody will close.

    Taco also writes short control lines down the live connection, which
    the satellite reads between frames (older firmware ignores them):
    `STATE <idle|listening|thinking|speaking> [here|elsewhere]`, `MUTE on|off`
    `UPDATE` (check for firmware now) and `AFE on|off` (switch the board's
    speech front end, which restarts it; sent when `afe_file` changes, and
    on connect when it disagrees with the handshake). `on_connect` runs for
    each new connection, so the caller can resend the current state.

    A satellite reports its own mute button the same way, on a short
    connection: `TACO-EVENT <name> <token> MUTE on|off`, answered `OK`
    (or `LEVELS ...`, per-channel levels, logged for diagnosis).
    While muted it streams silence, so this is only for the log.

    Firmware updates use the same port, token and allow list: a satellite
    asks with `TACO-OTA <name> <token> <running version>` and gets back
    `NONE`, or `IMAGE <size> <sha256> <version>` and the image itself, when
    `firmware` holds a build with a different version. The image has the
    Wi-Fi password and token compiled in, so it is only ever served here.
    """

    sample_rate = 16000   # fixed by the satellite protocol

    def __init__(self, name: str, port: int, token: str, allow: list[str] | None = None,
                 host: str = "0.0.0.0", ready: threading.Event | None = None,
                 firmware: str | Path | None = None, on_connect=None,
                 afe_file: str | Path | None = None):
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
        self.firmware = Path(firmware) if firmware else None
        self._fw_mtime: float | None = None
        self.on_connect = on_connect
        self.button_muted: bool | None = None   # unknown until the satellite says
        # What the live connection's handshake said: front end on, channels.
        self.afe: str | None = None
        self.channels = 1
        self.speaker = False   # the board can play replies (spk=1)
        self.second = SatelliteChannel(self, ready)
        # Holds "on" or "off" to switch the speech front end (deploy/satellite-afe.sh).
        self.afe_file = Path(afe_file) if afe_file else None
        self._afe_want = self._afe_wanted()
        # Control lines go out on their own thread, so a slow socket never
        # holds up the voice loop.
        self._out: queue.Queue[str] = queue.Queue(maxsize=50)

    def start(self) -> None:
        self._server = socket.create_server((self.host, self.port))
        self.port = self._server.getsockname()[1]   # when bound to port 0 (tests)
        threading.Thread(target=self._accept, daemon=True, name=f"sat-{self.name}").start()
        threading.Thread(target=self._send_lines, daemon=True, name=f"sat-{self.name}-out").start()
        self._fw_mtime = self._firmware_mtime()
        log.info("satellite %r: listening on port %d", self.name, self.port)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def live(self) -> bool:
        return self.connected   # a disconnect is logged on its own

    def send_line(self, line: str) -> None:
        """Queue a control line for the live connection; dropped if there's
        none, or if the queue is full."""
        if self._conn is None:
            return
        try:
            self._out.put_nowait(line)
        except queue.Full:
            pass

    def send_audio(self, pcm: bytes) -> bool:
        """Queue reply audio (16 kHz mono s16) for the board's speaker, as
        `AUDIO <n>` plus the bytes, in order with the control lines. Waits
        briefly rather than drop audio when the queue is full."""
        if self._conn is None:
            return False
        try:
            self._out.put(b"AUDIO %d\n" % len(pcm) + pcm, timeout=1.0)
            return True
        except queue.Full:
            return False

    def _send_lines(self) -> None:
        while True:
            item = self._out.get()
            conn = self._conn
            if conn is None:
                continue
            try:
                conn.sendall(item if isinstance(item, bytes) else f"{item}\n".encode())
            except OSError as e:
                what = "audio" if isinstance(item, bytes) else repr(item)
                log.info("satellite %r: %s not sent: %s", self.name, what, e)

    def _firmware_mtime(self) -> float | None:
        try:
            return self.firmware.stat().st_mtime if self.firmware else None
        except OSError:
            return None

    def check_firmware(self) -> None:
        """Tell the satellite to update as soon as a new image lands (rather
        than at its next 15-minute check)."""
        mtime = self._firmware_mtime()
        if mtime == self._fw_mtime:
            return
        self._fw_mtime = mtime
        if mtime is not None and self.connected:
            log.info("satellite %r: new firmware image; asking it to update", self.name)
            self.send_line("UPDATE")

    def _afe_wanted(self) -> str | None:
        try:
            want = self.afe_file.read_text().strip() if self.afe_file else ""
        except OSError:
            return None
        return want if want in ("on", "off") else None

    def check_afe(self) -> None:
        """Ask the board to switch its speech front end when the AFE file
        disagrees with what its handshake said. Firmware that predates the
        front end says nothing, and a board whose front end failed to start
        says afe=failed; both are left alone, so a board never restarts in
        a loop."""
        want = self._afe_wanted()
        if want != self._afe_want:
            self._afe_want = want
            if want:
                log.info("satellite %r: speech front end wanted %s", self.name, want)
        if want and self.connected and self.afe in ("on", "off") and self.afe != want:
            log.info("satellite %r: switching its speech front end %s (it restarts)", self.name, want)
            self.send_line(f"AFE {want}")
            self.afe = want   # the next handshake says what took

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

    def _handshake(self, conn: socket.socket) -> list[str] | None:
        """The handshake line's words, if the token is right."""
        conn.settimeout(5.0)
        line = b""
        while not line.endswith(b"\n") and len(line) < 256:
            byte = conn.recv(1)
            if not byte:
                return None
            line += byte
        parts = line.decode(errors="replace").split()
        ok = ((len(parts) >= 3 and parts[0] == "TACO") or (len(parts) == 4 and parts[0] == "TACO-OTA")
              or (len(parts) >= 4 and parts[0] == "TACO-EVENT"))
        return parts if ok and hmac.compare_digest(parts[2].encode(), self._token.encode()) else None

    def _event(self, words: list[str]) -> None:
        if words[:1] == ["MUTE"] and len(words) == 2:
            muted = words[1] == "on"
            if muted != self.button_muted:
                log.info("satellite %r: mute button %s", self.name, "on (mic muted)" if muted else "off")
            self.button_muted = muted
        elif words[:1] == ["LEVELS"]:
            # Per-channel "rms/peak" dBFS, for checking the board's channel layout.
            log.info("satellite %r: channel levels (rms/peak dBFS) %s", self.name, " ".join(words[1:]))
        else:
            log.warning("satellite %r: unknown event %r", self.name, " ".join(words))

    def _serve_update(self, conn: socket.socket, ip: str, running: str) -> None:
        """Answer a firmware check: the image if its version differs."""
        image = self.firmware.read_bytes() if self.firmware and self.firmware.is_file() else b""
        version = image_version(image)
        if not version or version == running:
            conn.sendall(b"NONE\n")
            return
        log.info("satellite %r: sending firmware %s to %s (running %s)", self.name, version, ip, running)
        conn.settimeout(60.0)
        conn.sendall(f"IMAGE {len(image)} {hashlib.sha256(image).hexdigest()} {version}\n".encode())
        conn.sendall(image)

    def _serve(self, conn: socket.socket, ip: str) -> None:
        try:
            if not self._allowed(ip):
                log.warning("satellite %r: refused %s (not in allow list)", self.name, ip)
                return
            if not (parts := self._handshake(conn)):
                log.warning("satellite %r: bad handshake from %s", self.name, ip)
                return
            if parts[0] == "TACO-OTA":
                self._serve_update(conn, ip, parts[3])
                return   # never touches the live audio connection
            if parts[0] == "TACO-EVENT":
                self._event(parts[3:])
                conn.sendall(b"OK\n")
                return
            conn.settimeout(None)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Control lines give up rather than block on a stuck satellite.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", 2, 0))
            opts = dict(w.split("=", 1) for w in parts[3:] if "=" in w)
            channels = 2 if opts.get("ch") == "2" else 1
            with self._lock:
                self.last_frame = time.monotonic()   # don't count the time it was away
                self.second.last_frame = self.last_frame
                old, self._conn = self._conn, conn
                self.afe, self.channels = opts.get("afe"), channels
                self.speaker = opts.get("spk") == "1"
            if old:
                _close(old)
            log.info("satellite %r: connected from %s%s", self.name, ip,
                     (f" (speech front end {self.afe}, {channels} ch{', speaker' if self.speaker else ''})"
                      if self.afe else ""))
            if self.on_connect:
                self.on_connect(self)
            stream = conn.makefile("rb")
            frame_bytes = FRAME_SAMPLES * 2 * channels
            while len(chunk := stream.read(frame_bytes)) == frame_bytes:
                if self._conn is not conn:
                    break   # replaced by a newer connection
                if channels == 1:
                    self._put(chunk)
                else:
                    both = np.frombuffer(chunk, dtype=np.int16)
                    self._put(both[0::2].tobytes())
                    self.second._put(both[1::2].tobytes())
        except OSError as e:
            log.info("satellite %r: %s", self.name, e)
        finally:
            with self._lock:
                if self._conn is conn:
                    self._conn = None
                    log.warning("satellite %r: disconnected", self.name)
            _close(conn)


def image_version(image: bytes) -> str | None:
    """The version in an ESP-IDF app image (its esp_app_desc_t), or None if
    it isn't one. The descriptor follows the 24-byte image header and the
    first 8-byte segment header."""
    if len(image) < 80 or image[0] != 0xE9 or image[32:36] != (0xABCD5432).to_bytes(4, "little"):
        return None
    return image[48:80].split(b"\0")[0].decode(errors="replace") or None


def _close(conn: socket.socket) -> None:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    conn.close()


def resample(audio: np.ndarray, rate: int, to: int = 16000) -> np.ndarray:
    """Linear resampling of int16 mono audio; plenty for speech and chimes."""
    if rate == to or len(audio) == 0:
        return audio.astype(np.int16)
    n = int(round(len(audio) * to / rate))
    t = np.arange(n) * (rate / to)
    return np.interp(t, np.arange(len(audio)), audio.astype(np.float32)).astype(np.int16)


class SatelliteSpeaker:
    """Plays clips on a satellite's own speaker, with the same play / wait /
    stop as Speaker. Audio goes down the satellite's connection in 100 ms
    chunks, paced in real time a little ahead (LEAD_S), so the board's buffer
    never overflows and stop() leaves little to drop. wait() returns once the
    board should have played everything."""

    CHUNK_S = 0.1
    LEAD_S = 0.4
    BOARD_LATENCY_S = 0.15   # its buffer and DMA, after the last chunk is sent

    def __init__(self, sat: "NetworkMicrophone"):
        self.sat = sat
        self._clips: queue.Queue[tuple[np.ndarray, int]] = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        self._stopping = threading.Event()
        self._end = 0.0   # monotonic time the board will have played everything sent
        threading.Thread(target=self._run, daemon=True, name=f"spk-{sat.name}").start()

    def play(self, audio: np.ndarray, sample_rate: int) -> None:
        self._idle.clear()
        self._clips.put((audio, sample_rate))

    def wait(self) -> None:
        self._clips.join()
        self._idle.wait()
        time.sleep(max(0.0, self._end + self.BOARD_LATENCY_S - time.monotonic()))

    def stop(self) -> None:
        if self._idle.is_set() and time.monotonic() >= self._end:
            return
        self._stopping.set()
        self.sat.send_line("STOP")
        self._end = 0.0

    def _run(self) -> None:
        chunk = int(16000 * self.CHUNK_S)
        while True:
            audio, rate = self._clips.get()
            try:
                pcm = resample(audio, rate)
                for i in range(0, len(pcm), chunk):
                    if self._stopping.is_set():
                        break
                    ahead = self._end - time.monotonic()
                    if ahead > self.LEAD_S:
                        time.sleep(ahead - self.LEAD_S)   # keep only LEAD_S queued on the board
                    piece = pcm[i:i + chunk]
                    if not self.sat.send_audio(piece.tobytes()):
                        break   # disconnected: nothing to wait for
                    self._end = max(time.monotonic(), self._end) + len(piece) / 16000
            finally:
                self._clips.task_done()
                if self._clips.empty():
                    self._stopping.clear()
                    self._idle.set()


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
                         # WirePlumber would otherwise save and restore volumes per media
                         # role, which every pw-play shares: music ducked to 0.2 became the
                         # volume of Taco's voice (2026-11-09).
                         "-P", "{ state.restore-props = false }",
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
