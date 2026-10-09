"""Scenes: named sets of actions ("good night", "movie time").

Each action is one of:
  {"type": "devices", "target": "everything" | "<room or device>", "on": bool}
  {"type": "devices", "ids": [1, 2], "on": bool, "level": 40}
  {"type": "briefing"}       speak the morning briefing
  {"type": "night"}          screen to night mode
  {"type": "media_stop"}     stop music/radio
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .intents import normalize

DEFAULT_SCENES: dict[str, list[dict[str, Any]]] = {
    "good night": [{"type": "devices", "target": "everything", "on": False},
                   {"type": "media_stop"}, {"type": "night"}],
    "good morning": [{"type": "briefing"}],
    "i'm leaving": [{"type": "devices", "target": "everything", "on": False}, {"type": "media_stop"}],
}

# Spoken variations (normalized) -> scene name.
SCENE_ALIASES = {
    "goodnight": "good night", "night night": "good night", "bedtime": "good night",
    "im leaving": "i'm leaving", "i am leaving": "i'm leaving", "we re leaving": "i'm leaving",
    "heading out": "i'm leaving", "i m heading out": "i'm leaving", "goodbye": "i'm leaving",
}


def scene_key(name: str) -> str:
    """Comparable form of a scene name: 'I'm leaving!' -> 'i m leaving'."""
    key = normalize(name)
    for suffix in (" scene", " mode"):
        key = key.removesuffix(suffix)
    return normalize(SCENE_ALIASES.get(key, key))


class SceneStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS scenes (
                    name TEXT PRIMARY KEY,
                    actions TEXT NOT NULL,
                    created_at REAL NOT NULL
                )""")
            empty = self._conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == 0
            if empty:
                self._conn.executemany(
                    "INSERT INTO scenes (name, actions, created_at) VALUES (?, ?, ?)",
                    [(n, json.dumps(a), time.time()) for n, a in DEFAULT_SCENES.items()])
            self._conn.commit()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT name, actions FROM scenes ORDER BY name").fetchall()
        return [{"name": r[0], "actions": json.loads(r[1])} for r in rows]

    def get(self, name: str) -> dict[str, Any] | None:
        key = scene_key(name)
        return next((s for s in self.all() if scene_key(s["name"]) == key), None)

    def save(self, name: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scenes (name, actions, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET actions = excluded.actions",
                (name.strip().lower(), json.dumps(actions), time.time()))
            self._conn.commit()
        return self.get(name)  # type: ignore[return-value]

    def delete(self, name: str) -> bool:
        s = self.get(name)
        if not s:
            return False
        with self._lock:
            self._conn.execute("DELETE FROM scenes WHERE name = ?", (s["name"],))
            self._conn.commit()
        return True
