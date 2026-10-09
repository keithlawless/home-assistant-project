"""SQLite storage. Phase 2 needs only settings; the device registry,
timers and lists add their tables here in later phases."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from taco.config import TACO_HOME

from .timers import TimerStore

SETTINGS_DEFAULTS: dict[str, Any] = {
    "screensaver_minutes": 5,
    "units": "fahrenheit",        # or "celsius"
    "clock_24h": False,
    # Filled in from IP geolocation on first start; editable in Settings.
    "location": None,             # {"name", "lat", "lon", "timezone"}
    "rooms": ["Kitchen", "Living Room", "Dining Room", "Family Room", "Bedroom"],
    "voice": None,                # "engine:voice" from taco.voices; None = config default
    "wake_sensitivity": "medium", # low | medium | high
    "mic_muted": False,           # "stop listening": wake word ignored until unmuted
    "vacation_mode": False,       # random lamps in the evening while away
    # Screen goes fully dark when idle during these hours (instead of the clock).
    "night_hours": {"enabled": False, "start": "22:30", "end": "06:30"},
    # Power the panel off in night mode (instead of black). Off until a touch
    # is confirmed to wake it on this hardware.
    "night_screen_off": False,
}


class Database:
    def __init__(self, path: Path | None = None):
        path = path or TACO_HOME / "data" / "taco.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection and lock, shared with the timer and device tables.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.conn.commit()
        self.timers = TimerStore(self.conn, self.lock)

    def settings(self) -> dict[str, Any]:
        with self.lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        return {**SETTINGS_DEFAULTS, **{k: json.loads(v) for k, v in rows}}

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - set(SETTINGS_DEFAULTS)
        if unknown:
            raise KeyError(f"unknown settings: {', '.join(sorted(unknown))}")
        with self.lock:
            self.conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v)) for k, v in changes.items()],
            )
            self.conn.commit()
        return self.settings()
