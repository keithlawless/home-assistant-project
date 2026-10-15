"""On-screen notification cards: they stack and stay until dismissed.

Sources: the outside webhook (webhook.py; e.g. Blink motion through an Alexa
routine), the Pi health monitor, and local scripts via /api/notifications.
A card with a `key` absorbs repeats of the same event (count goes up)
instead of stacking duplicates.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

KINDS = {"info", "warning", "camera", "health"}
MERGE_S = 120.0            # repeats of a keyed event within this window merge
MAX_FROM_WEBHOOK = 10      # outside senders can't bury the screen in cards

_COLUMNS = "id, kind, title, body, sender, key, count, created, updated, resolved"


def in_window(now: datetime, start: str, end: str) -> bool:
    """True if `now` falls in the daily HH:MM window [start, end), which may
    cross midnight (22:00-06:00)."""
    def mins(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    t, a, b = now.hour * 60 + now.minute, mins(start), mins(end)
    return a <= t < b if a <= b else t >= a or t < b


class NotificationStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock, clock=time.time):
        self._conn = conn
        self._lock = lock
        self._clock = clock
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    sender TEXT,
                    key TEXT,
                    count INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    resolved REAL
                )""")
            self._conn.commit()

    def active(self) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM notifications ORDER BY updated DESC, id DESC").fetchall()
        return [_row(r) for r in rows]

    def add(self, kind: str, title: str, body: str = "", *, key: str | None = None,
            sender: str | None = None, merge_s: float | None = MERGE_S) -> dict[str, Any]:
        """Add a card, or fold it into the card with the same key if that one
        was updated within `merge_s` seconds (None: always fold)."""
        kind = kind if kind in KINDS else "info"
        now = self._clock()
        with self._lock:
            row = None
            if key:
                row = self._conn.execute(
                    "SELECT id, updated FROM notifications WHERE key = ? ORDER BY updated DESC LIMIT 1",
                    (key,)).fetchone()
            if row and (merge_s is None or now - row[1] <= merge_s):
                self._conn.execute(
                    "UPDATE notifications SET title = ?, body = ?, count = count + 1, updated = ?, "
                    "resolved = NULL WHERE id = ?", (title, body, now, row[0]))
                card_id = row[0]
            else:
                card_id = self._conn.execute(
                    "INSERT INTO notifications (kind, title, body, sender, key, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, title, body, sender, key, now, now)).lastrowid
                if sender:
                    # Over the cap, the oldest outside cards make room.
                    self._conn.execute(
                        "DELETE FROM notifications WHERE id IN (SELECT id FROM notifications "
                        "WHERE sender IS NOT NULL ORDER BY updated DESC, id DESC LIMIT -1 OFFSET ?)",
                        (MAX_FROM_WEBHOOK,))
            self._conn.commit()
            r = self._conn.execute(f"SELECT {_COLUMNS} FROM notifications WHERE id = ?", (card_id,)).fetchone()
        return _row(r)

    def resolve(self, key: str) -> bool:
        """Mark the card for `key` as no longer happening (it stays until dismissed)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE notifications SET resolved = ? WHERE key = ? AND resolved IS NULL",
                (self._clock(), key))
            self._conn.commit()
        return cur.rowcount > 0

    def dismiss(self, card_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM notifications WHERE id = ?", (card_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def dismiss_all(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM notifications")
            self._conn.commit()
        return cur.rowcount


def _row(r) -> dict[str, Any]:
    return dict(zip(("id", "kind", "title", "body", "sender", "key", "count", "created", "updated",
                     "resolved"), r))
