"""Schedules (time or sun based automations) and vacation mode.

A schedule fires once per day at its trigger time, on the chosen weekdays:
  trigger: {"at": "18:30"} or {"sun": "sunset" | "sunrise", "offset_min": -15}
  days:    [0..6] with 0 = Monday; empty = every day
  actions: scene actions (see scenes.py) plus
           {"type": "scene", "name": "good night"}
           {"type": "alarm", "label": "work"}       ring now
           {"type": "say", "text": "Trash goes out tonight"}
These run in taco-core, so they work without the LLM or the internet
(sun times come from the last weather fetch).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

TICK_S = 20
CATCH_UP_S = 10 * 60   # a trigger missed by up to this long (e.g. a restart) still fires
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    days TEXT NOT NULL DEFAULT '[]',
                    actions TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run TEXT
                )""")
            self._conn.commit()

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, trigger, days, actions, enabled, last_run FROM schedules ORDER BY id").fetchall()
        return [{"id": r[0], "name": r[1], "trigger": json.loads(r[2]), "days": json.loads(r[3]),
                 "actions": json.loads(r[4]), "enabled": bool(r[5]), "last_run": r[6]} for r in rows]

    def add(self, name: str, trigger: dict, days: list[int], actions: list[dict]) -> dict[str, Any]:
        validate_trigger(trigger)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO schedules (name, trigger, days, actions) VALUES (?, ?, ?, ?)",
                (name, json.dumps(trigger), json.dumps(sorted(set(days))), json.dumps(actions)))
            self._conn.commit()
        return next(s for s in self.all() if s["id"] == cur.lastrowid)

    def update(self, schedule_id: int, **fields: Any) -> None:
        if "trigger" in fields:
            validate_trigger(fields["trigger"])
        for key in ("trigger", "days", "actions"):
            if key in fields:
                fields[key] = json.dumps(fields[key])
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE schedules SET {cols} WHERE id = ?", (*fields.values(), schedule_id))
            self._conn.commit()

    def delete(self, schedule_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            self._conn.commit()


def validate_trigger(trigger: dict) -> None:
    if "at" in trigger:
        datetime.strptime(trigger["at"], "%H:%M")
    elif trigger.get("sun") not in ("sunrise", "sunset"):
        raise ValueError('trigger needs {"at": "HH:MM"} or {"sun": "sunrise"|"sunset"}')


def trigger_time(trigger: dict, day: datetime, sun: dict[str, str] | None) -> datetime | None:
    """When a trigger fires on `day` (local time), or None if sun times are unknown."""
    if "at" in trigger:
        h, m = map(int, trigger["at"].split(":"))
        return day.replace(hour=h, minute=m, second=0, microsecond=0)
    if not sun or not sun.get(trigger["sun"]):
        return None
    base = datetime.fromisoformat(sun[trigger["sun"]])
    base = day.replace(hour=base.hour, minute=base.minute, second=0, microsecond=0)
    return base + timedelta(minutes=int(trigger.get("offset_min", 0)))


def describe(schedule: dict[str, Any]) -> str:
    """'every weekday at 6:30 AM', 'every day 15 minutes before sunset'."""
    days = schedule["days"]
    if not days or len(days) == 7:
        when = "every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "every weekday"
    elif days == [5, 6]:
        when = "every weekend"
    else:
        when = "every " + ", ".join(DAY_NAMES[d] for d in days)
    t = schedule["trigger"]
    if "at" in t:
        clock = datetime.strptime(t["at"], "%H:%M").strftime("%-I:%M %p").replace(":00 ", " ")
        return f"{when} at {clock}"
    off = int(t.get("offset_min", 0))
    if off == 0:
        return f"{when} at {t['sun']}"
    return f"{when} {abs(off)} minutes {'before' if off < 0 else 'after'} {t['sun']}"


class Scheduler:
    def __init__(self, store: ScheduleStore, run_actions: Callable[[list[dict]], Awaitable[None]],
                 sun: Callable[[], dict[str, str] | None], vacation: Callable[[], bool],
                 vacation_devices: Callable[[], list[int]], set_power: Callable[[list[int], bool], Awaitable[None]]):
        self.store = store
        self._run_actions = run_actions
        self._sun = sun
        self._vacation = vacation
        self._vacation_devices = vacation_devices
        self._set_power = set_power
        self._vacation_next = 0.0
        self._vacation_on: set[int] = set()

    async def loop(self) -> None:
        while True:
            try:
                await self.tick(datetime.now())
                await self.vacation_tick(datetime.now())
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self, now: datetime) -> list[str]:
        """Run due schedules; returns the names that fired (for tests)."""
        fired = []
        today = now.date().isoformat()
        for s in self.store.all():
            if not s["enabled"] or s["last_run"] == today:
                continue
            if s["days"] and now.weekday() not in s["days"]:
                continue
            at = trigger_time(s["trigger"], now, self._sun())
            if at is None or not (at <= now < at + timedelta(seconds=CATCH_UP_S)):
                continue
            self.store.update(s["id"], last_run=today)
            log.info("schedule %r firing", s["name"])
            fired.append(s["name"])
            await self._run_actions(s["actions"])
        return fired

    async def vacation_tick(self, now: datetime) -> None:
        """Between sunset and about 11 PM, switch lamps on and off at random
        so the house looks lived in. Everything goes off for the night."""
        if not self._vacation():
            if self._vacation_on:
                self._vacation_on.clear()
            return
        sun = self._sun()
        start = trigger_time({"sun": "sunset", "offset_min": -20}, now, sun) or now.replace(hour=18, minute=30)
        end = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if not (start <= now < end):
            if self._vacation_on:
                await self._set_power(sorted(self._vacation_on), False)
                self._vacation_on.clear()
            return
        if time.time() < self._vacation_next:
            return
        devices = self._vacation_devices()
        if not devices:
            return
        device = random.choice(devices)
        turn_on = device not in self._vacation_on or random.random() < 0.3
        await self._set_power([device], turn_on)
        (self._vacation_on.add if turn_on else self._vacation_on.discard)(device)
        self._vacation_next = time.time() + random.uniform(15, 50) * 60
        log.info("vacation mode: device %s %s", device, "on" if turn_on else "off")
