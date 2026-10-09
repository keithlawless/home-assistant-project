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
}


class Database:
    def __init__(self, path: Path | None = None):
        path = path or TACO_HOME / "data" / "taco.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.commit()
        self.timers = TimerStore(self._conn, self._lock)

    def settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        return {**SETTINGS_DEFAULTS, **{k: json.loads(v) for k, v in rows}}

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - set(SETTINGS_DEFAULTS)
        if unknown:
            raise KeyError(f"unknown settings: {', '.join(sorted(unknown))}")
        with self._lock:
            self._conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v)) for k, v in changes.items()],
            )
            self._conn.commit()
        return self.settings()
