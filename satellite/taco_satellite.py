#!/usr/bin/env python3
"""Taco mic satellite: stream a room's microphone to Taco over the LAN.

Runs on a small Linux box (e.g. a Pi Zero 2 W) in another room. It records
16 kHz mono s16 audio with arecord and sends it to taco-voice, which does the
wake word, VAD and STT. Stdlib only, so it needs no venv.

Settings come from /etc/taco-satellite.conf (KEY=value lines) or the
environment: TACO_HOST, TACO_PORT, SAT_NAME, SAT_TOKEN, and optionally
SAT_DEVICE (an ALSA device such as plughw:1,0; see `arecord -l`) or
SAT_COMMAND (any command writing raw 16 kHz mono s16 to stdout, for testing).
"""

from __future__ import annotations

import logging
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path

log = logging.getLogger("taco-satellite")

CONF = Path(os.environ.get("SAT_CONF", "/etc/taco-satellite.conf"))
CHUNK = 2560   # 80 ms at 16 kHz s16


def load_settings() -> dict[str, str]:
    settings: dict[str, str] = {}
    try:
        lines = CONF.read_text().splitlines()
    except OSError:
        lines = []   # under systemd the settings arrive via EnvironmentFile
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip('"').strip("'")
    settings.update({k: v for k, v in os.environ.items() if k.startswith(("TACO_", "SAT_")) and v})
    for key in ("TACO_HOST", "SAT_TOKEN"):
        if not settings.get(key):
            raise SystemExit(f"{key} is not set (see {CONF})")
    return settings


def record_command(settings: dict[str, str]) -> list[str]:
    if cmd := settings.get("SAT_COMMAND"):
        return shlex.split(cmd)
    device = ["-D", settings["SAT_DEVICE"]] if settings.get("SAT_DEVICE") else []
    return ["arecord", "-q", *device, "-r", "16000", "-c", "1", "-f", "S16_LE", "-t", "raw"]


def stream_once(settings: dict[str, str]) -> None:
    """Connect, then pipe audio until either side fails."""
    host, port = settings["TACO_HOST"], int(settings.get("TACO_PORT", "10700"))
    name = settings.get("SAT_NAME", socket.gethostname()).replace(" ", "_")
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.settimeout(None)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.sendall(f"TACO {name} {settings['SAT_TOKEN']}\n".encode())
        log.info("connected to %s:%d", host, port)
        rec = subprocess.Popen(record_command(settings), stdout=subprocess.PIPE)
        assert rec.stdout
        try:
            while chunk := rec.stdout.read(CHUNK):
                sock.sendall(chunk)
            log.warning("recorder exited (%s)", rec.wait())
        finally:
            rec.kill()
            rec.wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    delay = 1.0
    while True:
        started = time.monotonic()
        try:
            stream_once(settings)
        except OSError as e:
            log.warning("connection: %s", e)
        # Back off while Taco is unreachable; reset once a stream ran a while.
        delay = 1.0 if time.monotonic() - started > 30 else min(delay * 2, 30.0)
        time.sleep(delay)


if __name__ == "__main__":
    main()
