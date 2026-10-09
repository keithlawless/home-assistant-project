"""Shopping and to-do lists (any named list, really)."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from typing import Any

# Spoken variations -> canonical list name.
LIST_ALIASES = {
    "grocery": "shopping", "groceries": "shopping", "grocery store": "shopping", "store": "shopping",
    "shopping": "shopping", "todo": "to do", "to do": "to do", "to-do": "to do", "chores": "to do",
}


def canonical(name: str | None) -> str:
    name = (name or "").strip().lower().removeprefix("the ").removeprefix("my ").strip()
    return LIST_ALIASES.get(name, name or "shopping")


class ListStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS list_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    list TEXT NOT NULL,
                    text TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )""")
            self._conn.commit()

    def items(self, name: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, list, text, done FROM list_items"
        args: tuple = ()
        if name:
            sql += " WHERE list = ?"
            args = (canonical(name),)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY done, created_at", args).fetchall()
        return [{"id": r[0], "list": r[1], "text": r[2], "done": bool(r[3])} for r in rows]

    def names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT list FROM list_items ORDER BY list").fetchall()
        found = [r[0] for r in rows]
        return ["shopping", "to do", *[n for n in found if n not in ("shopping", "to do")]]

    def add(self, name: str | None, texts: list[str]) -> list[str]:
        """Add items, skipping ones already on the list. Returns what was added."""
        name = canonical(name)
        existing = {i["text"].lower() for i in self.items(name) if not i["done"]}
        added = [t.strip() for t in texts if t.strip() and t.strip().lower() not in existing]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO list_items (list, text, created_at) VALUES (?, ?, ?)",
                [(name, t, time.time()) for t in added])
            self._conn.commit()
        return added

    def remove(self, name: str | None, texts: list[str]) -> list[str]:
        """Remove items by (fuzzy) text. Returns the items removed."""
        removed = []
        for want in texts:
            want = want.strip().lower()
            for item in self.items(name):
                have = item["text"].lower()
                if want and (want == have or want in have or have in want):
                    self.delete(item["id"])
                    removed.append(item["text"])
                    break
        return removed

    def delete(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM list_items WHERE id = ?", (item_id,))
            self._conn.commit()

    def set_done(self, item_id: int, done: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE list_items SET done = ? WHERE id = ?", (int(done), item_id))
            self._conn.commit()

    def clear(self, name: str | None, only_done: bool = False) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM list_items WHERE list = ?" + (" AND done = 1" if only_done else ""),
                (canonical(name),))
            self._conn.commit()
        return cur.rowcount


def split_items(text: str) -> list[str]:
    """'milk, eggs and a dozen apples' -> ['milk', 'eggs', 'a dozen apples']."""
    parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", text)
    return [p.strip() for p in parts if p.strip()]
