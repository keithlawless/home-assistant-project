"""Taco configuration.

Defaults live here. Overrides come from /srv/taco/config.toml (or $TACO_CONFIG),
merged one table deep, so a file only needs the keys it changes.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

TACO_HOME = Path(os.environ.get("TACO_HOME", "/srv/taco"))

DEFAULTS: dict = {
    "audio": {
        # None means the PipeWire default device. Set a PipeWire node name
        # (from `wpctl status` / `pw-cli ls Node`) to pin a device, e.g. when
        # the speakerphone arrives.
        "input_device": None,
        "input_name": "kitchen",   # room name for the local mic in logs
        "output_device": None,
        "sample_rate": 16000,
        # Listen for the wake word while Taco talks. Needs a mic with echo
        # cancellation (e.g. a USB speakerphone), or Taco hears itself.
        "barge_in": False,
        # Mic satellites in other rooms (satellite/README.md), e.g.
        #   [[audio.satellites]]
        #   name = "living room"
        #   port = 10700
        #   token = "..."               # shared secret; keep it out of the repo
        #   allow = ["192.168.4.0/24"]  # peers allowed to connect
        "satellites": [],
    },
    "wake": {
        # Built-in name, or a path to a custom model such as
        # /srv/taco/models/wakeword/hey_taco.onnx (see wakeword/README.md).
        "model": "hey_jarvis",
        "threshold": 0.5,   # starting point; Settings → wake sensitivity overrides it
        # 80 ms frames in a row at or above the threshold. A real "Hey Taco"
        # holds for about four; TV audio has fired on a one-frame spike.
        "patience": 2,
        # Per-room tuning, keyed by mic name. A mic near a TV can ask for a
        # higher score, held for longer:
        #   [wake.rooms."living room"]
        #   offset = 0.1    # added to the threshold
        #   patience = 3    # overrides [wake] patience
        "rooms": {},
        # Keep a short clip of every wake and near miss, for tuning and
        # retraining (taco/voice/clips.py). Near misses are runs that peak at
        # or above near_miss without firing. Oldest clips go past max_clips.
        "save_clips": True,
        "clips_dir": str(TACO_HOME / "wake_clips"),
        "near_miss": 0.3,
        "max_clips": 1000,
    },
    "listen": {
        "start_timeout_s": 5.0,   # give up if no speech starts after the chime
        "end_silence_s": 0.8,     # this much silence ends the request
        "max_seconds": 12.0,
        "vad_aggressiveness": 2,  # webrtcvad 0-3
        # Satellites hear speech from across a room, too quietly for level 2,
        # which lost the end of requests from the living room.
        "satellite_vad_aggressiveness": 1,
        "followup_s": 6.0,        # listen again without wake word after a question
        "ack_phrase": "Working.", # spoken once the request is transcribed; "" to disable
    },
    "stt": {
        "model": "base.en",
        "compute_type": "int8",
        # Local names Whisper should expect (nearby towns, streets). The home
        # town from Settings is added automatically.
        "vocabulary": [],
    },
    "tts": {
        # Starting voice; the Settings screen (or "change your voice") overrides it.
        "engine": "piper",
        "voice": "en_US-lessac-medium",
    },
    "hermes": {
        "url": "http://127.0.0.1:8642/v1",
        "model": "hermes",
        "env_file": str(Path.home() / ".hermes" / ".env"),
        "timeout_s": 60.0,
        "history_ttl_s": 120.0,   # follow-up questions keep context this long
    },
    "core": {
        "url": "http://127.0.0.1:8700",
        # The API has no login, so by default only the Pi itself can reach it
        # (the kiosk, the voice loop and Hermes all run there). Set "0.0.0.0"
        # to open it to the LAN.
        "host": "127.0.0.1",
        "port": 8700,
    },
    "speakers": {
        # "Play on Sonos" lasts until "play here", this long with no music,
        # the speaker dropping off, or a taco-core restart.
        "idle_revert_min": 30,
    },
    "kasa": {
        # Only newer Kasa firmware needs TP-Link account credentials.
        "username": None,
        "password": None,
    },
    "calendar": {
        # Private iCal addresses (secret; keep them in /srv/taco/config.toml only).
        "ical_urls": [],
    },
    "notifications": {
        # Webhook notifications still wake the screen but don't chime.
        "quiet_hours": {"start": "22:00", "end": "06:00"},
        # Also pass webhook notifications (not health cards) to Chilli, which
        # sends them to Telegram. Empty url = off. Token: keep it out of the repo.
        "forward": {"url": "", "token": ""},
    },
    "health": {
        # Pi health cards: CPU (averaged over window_s), memory, temperature.
        # Each clears clear_margin below its limit. Throttling and
        # under-voltage always raise a card.
        "cpu_percent": 75,
        "memory_percent": 75,
        "temp_c": 75,              # Pi 5 soft-throttles at 80 °C
        "clear_margin": 5,
        "window_s": 120,
        "interval_s": 10,
    },
    "webhooks": {
        # Outside senders allowed to post notifications through the Cloudflare
        # tunnel (deploy/cloudflared/README.md). Empty = webhook off. e.g.
        #   [[webhooks.senders]]
        #   name  = "alexa"
        #   token = "..."   # openssl rand -hex 24; keep it out of the repo
        "senders": [],
    },
    "matter": {
        "url": "ws://127.0.0.1:5580/ws",
        # Needed only to set up brand-new Matter devices over Bluetooth.
        "wifi_ssid": None,
        "wifi_password": None,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    path = Path(os.environ.get("TACO_CONFIG", TACO_HOME / "config.toml"))
    if path.exists():
        with path.open("rb") as f:
            return _merge(DEFAULTS, tomllib.load(f))
    return copy.deepcopy(DEFAULTS)


def read_env_value(env_file: str, key: str) -> str | None:
    """Read one KEY=value line from a dotenv-style file."""
    try:
        for line in Path(env_file).read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None
