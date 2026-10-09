"""Resolve spoken targets ("the living room", "lamp one", "everything") to devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..intents import normalize

GENERIC = {"the", "light", "lights", "lamp", "lamps", "plug", "plugs", "in", "all", "everything",
           "devices", "device", "stuff", "please", "my", "of", "and", "s", "switch", "switches"}
EVERYTHING = {"everything", "all", "all lights", "all the lights", "all lamps", "all the lamps",
              "all devices", "all the devices", "all plugs", "all the plugs", "the house",
              "the whole house", "whole house", "lights", "the lights", "all of them", "everything off"}


@dataclass
class Match:
    scope: str                       # device | room | all
    ids: list[int] = field(default_factory=list)
    room: str | None = None
    ambiguous: bool = False          # several devices fit; ask which


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def _names(device: dict[str, Any]) -> list[list[str]]:
    return [_tokens(n) for n in [device["name"], *device.get("aliases", [])]]


def resolve(target: str, devices: list[dict[str, Any]], rooms: list[str]) -> Match | None:
    t = normalize(target).removeprefix("the ").strip()
    if not t or not devices:
        return None
    if t in EVERYTHING:
        return Match("all", [d["id"] for d in devices])
    words = t.split()

    # 1. A device whose full name (or alias) appears in what was said.
    best, best_len = [], 0
    for d in devices:
        for name in _names(d):
            if name and all(w in words for w in name):
                if len(name) > best_len:
                    best, best_len = [d], len(name)
                elif len(name) == best_len and d not in best:
                    best.append(d)
    if best:
        return Match("device", [d["id"] for d in best], ambiguous=len(best) > 1)

    # 2. A room, optionally narrowed by more words ("living room lamps" = the room).
    for room in sorted(rooms, key=len, reverse=True):
        room_words = _tokens(room)
        if room_words and " ".join(room_words) in " ".join(words):
            in_room = [d for d in devices if (d.get("room") or "").lower() == room.lower()]
            rest = [w for w in words if w not in room_words and w not in GENERIC]
            if rest:
                narrowed = [d for d in in_room if any(all(w in n for w in rest) for n in _names(d))]
                if narrowed:
                    return Match("device", [d["id"] for d in narrowed], room, ambiguous=len(narrowed) > 1)
            if in_room:
                return Match("room", [d["id"] for d in in_room], room)
            return Match("room", [], room)

    # 3. Part of a name ("lamp one", "the coffee maker").
    words = [w for w in words if w not in ("the", "please")]
    partial = [d for d in devices if any(all(w in n for w in words) for n in _names(d))]
    if partial:
        return Match("device", [d["id"] for d in partial], ambiguous=len(partial) > 1)
    return None


def guess_room(name: str, rooms: list[str]) -> str | None:
    """'Living Room Lamp One' -> 'Living Room'."""
    n = " ".join(_tokens(name))
    for room in sorted(rooms, key=len, reverse=True):
        if " ".join(_tokens(room)) in n:
            return room
    return None
