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
        "output_device": None,
        "sample_rate": 16000,
        # Listen for the wake word while Taco talks. Needs a mic with echo
        # cancellation (e.g. a USB speakerphone), or Taco hears itself.
        "barge_in": False,
    },
    "wake": {
        # Built-in name, or a path to a custom model such as
        # /srv/taco/models/wakeword/hey_taco.onnx (see wakeword/README.md).
        "model": "hey_jarvis",
        "threshold": 0.5,
    },
    "listen": {
        "start_timeout_s": 5.0,   # give up if no speech starts after the chime
        "end_silence_s": 0.8,     # this much silence ends the request
        "max_seconds": 12.0,
        "vad_aggressiveness": 2,  # webrtcvad 0-3
        "followup_s": 6.0,        # listen again without wake word after a question
        "ack_phrase": "Working.", # spoken once the request is transcribed; "" to disable
    },
    "stt": {
        "model": "base.en",
        "compute_type": "int8",
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
    },
    "kasa": {
        # Only newer Kasa firmware needs TP-Link account credentials.
        "username": None,
        "password": None,
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
