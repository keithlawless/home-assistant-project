#!/usr/bin/env python3
"""Bench test for a satellite's speaker: music, a reply mixed over it, and
what the mics hear meanwhile (to check echo cancellation).

It stands in for taco-voice on the satellite's port, so stop taco-voice
first and start it again after:

    systemctl --user stop taco-voice
    ~/taco/.venv/bin/python satellite/sat_music_test.py "living room" song.mp3 --record /tmp/sat.wav
    systemctl --user start taco-voice

It plays SECONDS of the file (any format ffmpeg reads; "sweep" plays a
100 Hz - 16 kHz tone sweep) as MUSIC, puts a reply chime over it halfway
through, and writes the mic stream to --record (one channel per stream
channel). It prints the mic level with and without music playing: with
echo cancellation on, the difference should be small.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time
import tomllib
import wave
from pathlib import Path

import numpy as np

FRAME = 1280   # mic samples per channel per frame, 16 kHz


def satellite(name: str, config: Path) -> dict:
    cfg = tomllib.loads(config.read_text())
    for s in cfg.get("audio", {}).get("satellites", []):
        if s["name"] == name:
            return s
    sys.exit(f"no satellite {name!r} in {config}")


def source(arg: str, rate: int, seconds: float) -> np.ndarray:
    if arg == "sweep":
        t = np.arange(int(rate * seconds)) / rate
        f0, f1 = 100.0, 16000.0
        phase = 2 * np.pi * f0 * seconds / np.log(f1 / f0) * (np.exp(t / seconds * np.log(f1 / f0)) - 1)
        return (np.sin(phase) * 8000).astype(np.int16)
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", arg, "-t", str(seconds), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        check=True, capture_output=True).stdout
    return np.frombuffer(pcm, dtype=np.int16)


def chime(rate: int) -> np.ndarray:
    t = np.arange(int(rate * 0.25)) / rate
    notes = [np.sin(2 * np.pi * f * t) * np.exp(-t * 8) for f in (784, 988, 1175)]
    return (np.concatenate(notes) * 12000).astype(np.int16)


def accept(server: socket.socket, token: str) -> tuple[socket.socket, dict]:
    """The satellite's audio connection; firmware checks and events are answered."""
    while True:
        conn, (ip, *_) = server.accept()
        line = b""
        while not line.endswith(b"\n"):
            line += conn.recv(1)
        words = line.decode().split()
        if len(words) < 3 or words[2] != token:
            conn.close()
            continue
        if words[0] == "TACO-OTA":
            conn.sendall(b"NONE\n")
            conn.close()
        elif words[0] == "TACO-EVENT":
            print("event:", " ".join(words[3:]))
            conn.sendall(b"OK\n")
            conn.close()
        elif words[0] == "TACO":
            opts = dict(w.split("=", 1) for w in words[3:] if "=" in w)
            print(f"satellite connected from {ip}: {opts}")
            return conn, opts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name")
    ap.add_argument("music", help='an audio file, or "sweep"')
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--volume", type=int, default=50)
    ap.add_argument("--record", type=Path)
    ap.add_argument("--config", type=Path, default=Path("/srv/taco/config.toml"))
    args = ap.parse_args()

    sat = satellite(args.name, args.config)
    server = socket.create_server(("0.0.0.0", sat["port"]))
    print(f"waiting for {args.name!r} on port {sat['port']} (it retries every few seconds)")
    conn, opts = accept(server, sat["token"])
    rate = int(opts.get("spk", "0"))
    rate = 16000 if rate == 1 else rate
    if not rate:
        sys.exit("this firmware has no speaker")
    channels = 2 if opts.get("ch") == "2" else 1
    music = source(args.music, rate, args.seconds)
    print(f"speaker at {rate} Hz; playing {len(music) / rate:.1f} s")

    mics: list[np.ndarray] = []
    stop = threading.Event()

    def record() -> None:
        stream = conn.makefile("rb")
        size = FRAME * 2 * channels
        while not stop.is_set() and len(chunk := stream.read(size)) == size:
            mics.append(np.frombuffer(chunk, dtype=np.int16).reshape(-1, channels))

    threading.Thread(target=record, daemon=True).start()
    conn.sendall(f"VOLUME {args.volume}\n".encode())
    time.sleep(3)   # mic level with nothing playing
    quiet_frames = len(mics)

    chunk, lead, end = rate // 10, 0.4, time.monotonic()
    reply_at = len(music) // 2
    for i in range(0, len(music), chunk):
        ahead = end - time.monotonic()
        if ahead > lead:
            time.sleep(ahead - lead)
        piece = music[i:i + chunk].tobytes()
        conn.sendall(b"MUSIC %d\n" % len(piece) + piece)
        if i <= reply_at < i + chunk:
            c = chime(rate).tobytes()
            conn.sendall(b"AUDIO %d\n" % len(c) + c)
        end = max(time.monotonic(), end) + len(piece) / 2 / rate
    time.sleep(max(0.0, end - time.monotonic()) + 0.5)
    music_frames = len(mics)
    conn.sendall(b"MSTOP\n")
    stop.set()
    conn.close()

    def dbfs(frames: list[np.ndarray]) -> str:
        if not frames:
            return "n/a"
        x = np.concatenate(frames).astype(np.float64)
        return " ".join(f"{20 * np.log10(np.sqrt(np.mean(x[:, c] ** 2)) / 32768 + 1e-9):.1f}" for c in range(channels))

    print(f"mic rms dBFS, quiet: {dbfs(mics[:quiet_frames])}")
    print(f"mic rms dBFS, music: {dbfs(mics[quiet_frames:music_frames])}")
    if args.record and mics:
        with wave.open(str(args.record), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(np.concatenate(mics).tobytes())
        print(f"mic stream written to {args.record}")


if __name__ == "__main__":
    main()
