"""Kitchen timers, alarms and reminders.

All three are rows with an `ends_at` time. A background tick moves due rows
to "ringing"; taco-voice plays the alarm and the UI shows a stop button
until someone stops it (or it auto-stops). Rows persist in SQLite, so a
reboot mid-countdown keeps the timer; anything that came due while Taco
was down rings as soon as it's back.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

AUTO_STOP_S = 10 * 60   # stop ringing after this long if nobody answers


@dataclass
class Timer:
    id: int
    kind: str                 # timer | alarm | reminder
    label: str | None
    duration_s: float | None  # timers only
    ends_at: float            # epoch seconds
    state: str                # running | paused | ringing
    remaining_s: float | None # while paused
    message: str | None       # reminders: what to say
    created_at: float
    rang_at: float | None = None

    def remaining(self, now: float | None = None) -> float:
        if self.state == "paused":
            return self.remaining_s or 0.0
        return max(0.0, self.ends_at - (now or time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "remaining": round(self.remaining(), 1)}

    def describe(self) -> str:
        """Spoken name: "the pasta timer", "your 7:30 alarm", "your reminder"."""
        if self.kind == "timer":
            return f"the {self.label} timer" if self.label else "the timer"
        if self.kind == "alarm":
            return f"your {self.label} alarm" if self.label else "your alarm"
        return "your reminder"


class TimerStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS timers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    label TEXT,
                    duration_s REAL,
                    ends_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    remaining_s REAL,
                    message TEXT,
                    created_at REAL NOT NULL,
                    rang_at REAL
                )""")
            self._conn.commit()

    # -- queries -------------------------------------------------------------

    def all(self) -> list[Timer]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, label, duration_s, ends_at, state, remaining_s, message, "
                "created_at, rang_at FROM timers ORDER BY ends_at").fetchall()
        timers = [Timer(*r) for r in rows]
        # Paused timers sort by what's left, not their stale ends_at.
        return sorted(timers, key=lambda t: t.remaining() if t.state == "paused" else t.ends_at - time.time())

    def get(self, timer_id: int) -> Timer | None:
        return next((t for t in self.all() if t.id == timer_id), None)

    def find(self, label: str | None = None, kind: str | None = None) -> list[Timer]:
        """Match by kind and by label (case-insensitive, either containing the other)."""
        out = []
        for t in self.all():
            if kind and t.kind != kind:
                continue
            if label:
                want, have = label.lower().strip(), (t.label or "").lower()
                if not have or (want not in have and have not in want):
                    continue
            out.append(t)
        return out

    def ringing(self) -> list[Timer]:
        return [t for t in self.all() if t.state == "ringing"]

    # -- changes -------------------------------------------------------------

    def _insert(self, kind: str, ends_at: float, label: str | None = None,
                duration_s: float | None = None, message: str | None = None) -> Timer:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO timers (kind, label, duration_s, ends_at, state, message, created_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (kind, label or None, duration_s, ends_at, message, time.time()))
            self._conn.commit()
            timer_id = cur.lastrowid
        return self.get(timer_id)  # type: ignore[return-value]

    def add_timer(self, seconds: float, label: str | None = None) -> Timer:
        if seconds <= 0:
            raise ValueError("timer length must be positive")
        return self._insert("timer", time.time() + seconds, label, duration_s=seconds)

    def add_alarm(self, at: float, label: str | None = None) -> Timer:
        return self._insert("alarm", at, label)

    def add_reminder(self, at: float, message: str) -> Timer:
        return self._insert("reminder", at, message=message)

    def _update(self, timer_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE timers SET {cols} WHERE id = ?", (*fields.values(), timer_id))
            self._conn.commit()

    def remove(self, timer_ids: list[int]) -> None:
        if not timer_ids:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM timers WHERE id = ?", [(i,) for i in timer_ids])
            self._conn.commit()

    def add_time(self, timer: Timer, seconds: float) -> Timer:
        if timer.state == "paused":
            self._update(timer.id, remaining_s=max(1.0, (timer.remaining_s or 0) + seconds))
        elif timer.state == "ringing":
            # "Add 2 minutes" while it's ringing restarts it for 2 minutes.
            self._update(timer.id, state="running", ends_at=time.time() + seconds, rang_at=None)
        else:
            self._update(timer.id, ends_at=max(time.time() + 1, timer.ends_at + seconds))
        return self.get(timer.id)  # type: ignore[return-value]

    def pause(self, timer: Timer) -> Timer:
        if timer.state == "running":
            self._update(timer.id, state="paused", remaining_s=timer.remaining())
        return self.get(timer.id)  # type: ignore[return-value]

    def resume(self, timer: Timer) -> Timer:
        if timer.state == "paused":
            self._update(timer.id, state="running", ends_at=time.time() + (timer.remaining_s or 0),
                         remaining_s=None)
        return self.get(timer.id)  # type: ignore[return-value]

    def stop_ringing(self) -> list[Timer]:
        stopped = self.ringing()
        self.remove([t.id for t in stopped])
        return stopped

    def tick(self) -> bool:
        """Advance states; return True if anything changed."""
        now = time.time()
        changed = False
        for t in self.all():
            if t.state == "running" and t.ends_at <= now:
                self._update(t.id, state="ringing", rang_at=now)
                changed = True
            elif t.state == "ringing" and t.rang_at and now - t.rang_at > AUTO_STOP_S:
                self.remove([t.id])
                changed = True
        return changed
