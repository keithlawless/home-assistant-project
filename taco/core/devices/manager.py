"""Device registry (SQLite) plus live state across Kasa and Matter."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from .base import DeviceError, DeviceState, Driver, Found
from .match import Match, guess_room, resolve

log = logging.getLogger(__name__)

POLL_S = 15


class Registry:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    protocol TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    address TEXT NOT NULL,
                    name TEXT NOT NULL,
                    room TEXT,
                    kind TEXT NOT NULL,
                    model TEXT,
                    capabilities TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    UNIQUE (protocol, native_id)
                )""")
            self._conn.commit()

    _COLS = "id, protocol, native_id, address, name, room, kind, model, capabilities, aliases"

    def _row(self, r: tuple) -> dict[str, Any]:
        d = dict(zip(self._COLS.split(", "), r))
        d["capabilities"] = json.loads(d["capabilities"])
        d["aliases"] = json.loads(d["aliases"])
        return d

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._COLS} FROM devices ORDER BY room, name").fetchall()
        return [self._row(r) for r in rows]

    def get(self, device_id: int) -> dict[str, Any] | None:
        return next((d for d in self.all() if d["id"] == device_id), None)

    def by_native(self, protocol: str, native_id: str) -> dict[str, Any] | None:
        return next((d for d in self.all() if d["protocol"] == protocol and d["native_id"] == native_id), None)

    def add(self, f: Found, name: str, room: str | None) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO devices (protocol, native_id, address, name, room, kind, model, capabilities, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f.protocol, f.native_id, f.address, name, room, f.kind, f.model,
                 json.dumps(f.capabilities), time.time()))
            self._conn.commit()
        return self.get(cur.lastrowid)  # type: ignore[return-value]

    def update(self, device_id: int, **fields: Any) -> dict[str, Any] | None:
        if "aliases" in fields:
            fields["aliases"] = json.dumps(fields["aliases"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE devices SET {cols} WHERE id = ?", (*fields.values(), device_id))
            self._conn.commit()
        return self.get(device_id)

    def remove(self, device_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            self._conn.commit()


class DeviceManager:
    def __init__(self, registry: Registry, drivers: dict[str, Driver],
                 rooms: Callable[[], list[str]], on_change: Callable[[], Awaitable[None]]):
        self.registry = registry
        self.drivers = drivers
        self._rooms = rooms
        self._on_change = on_change
        self.states: dict[int, DeviceState] = {}
        self.discovered: dict[str, Found] = {}  # unregistered, keyed "protocol:native_id"

    # -- views ------------------------------------------------------------------

    def payload(self) -> list[dict[str, Any]]:
        out = []
        for d in self.registry.all():
            s = self.states.get(d["id"])
            out.append({**d, "state": asdict(s) if s else {"online": None, "on": None, "level": None}})
        return out

    def found_payload(self) -> list[dict[str, Any]]:
        rooms = self._rooms()
        return [{"key": k, **asdict(f), "suggested_room": guess_room(f.name, rooms)}
                for k, f in self.discovered.items()]

    def resolve(self, target: str) -> Match | None:
        return resolve(target, self.registry.all(), self._rooms())

    # -- discovery and registry --------------------------------------------------

    async def scan(self) -> list[dict[str, Any]]:
        """Look for devices; refresh addresses of known ones; return the new ones."""
        results = await asyncio.gather(*(drv.discover() for drv in self.drivers.values()),
                                       return_exceptions=True)
        self.discovered = {}
        for result in results:
            if isinstance(result, BaseException):
                log.warning("discovery failed: %s", result)
                continue
            for f in result:
                known = self.registry.by_native(f.protocol, f.native_id)
                if known:
                    if known["address"] != f.address:
                        self.registry.update(known["id"], address=f.address)
                else:
                    self.discovered[f"{f.protocol}:{f.native_id}"] = f
        await self.refresh()
        return self.found_payload()

    def add(self, key: str, name: str | None = None, room: str | None = None) -> dict[str, Any]:
        f = self.discovered.pop(key, None)
        if f is None:
            raise KeyError(f"no discovered device {key}; scan first")
        name = (name or f.name).strip()
        room = room or guess_room(name, self._rooms())
        return self.registry.add(f, name, room)

    def add_found(self, found: list[Found]) -> None:
        """Remember freshly commissioned Matter endpoints as discovered."""
        for f in found:
            if not self.registry.by_native(f.protocol, f.native_id):
                self.discovered[f"{f.protocol}:{f.native_id}"] = f

    # -- control -------------------------------------------------------------------

    async def _apply(self, device: dict[str, Any], op: str, value: Any) -> DeviceState | None:
        drv = self.drivers[device["protocol"]]
        try:
            if op == "power":
                state = await drv.set_power(device["address"], value)
            else:
                state = await drv.set_level(device["address"], value)
        except DeviceError as e:
            log.warning("%s %s failed: %s", device["name"], op, e)
            self.states[device["id"]] = DeviceState(online=False)
            return None
        self.states[device["id"]] = state
        return state

    async def control(self, ids: list[int], op: str, value: Any) -> tuple[list[dict], list[dict]]:
        """Apply to several devices at once; return (succeeded, failed) devices."""
        devices = [d for i in ids if (d := self.registry.get(i))]
        if op == "level":
            devices = [d for d in devices if "level" in d["capabilities"]]
        results = await asyncio.gather(*(self._apply(d, op, value) for d in devices))
        await self._on_change()
        ok = [d for d, r in zip(devices, results) if r is not None]
        failed = [d for d, r in zip(devices, results) if r is None]
        return ok, failed

    async def refresh(self) -> None:
        devices = self.registry.all()

        async def one(d):
            try:
                self.states[d["id"]] = await self.drivers[d["protocol"]].state(d["address"])
            except DeviceError:
                self.states[d["id"]] = DeviceState(online=False)

        await asyncio.gather(*(one(d) for d in devices))
        await self._on_change()

    async def poll_loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("device refresh failed")
            await asyncio.sleep(POLL_S)
