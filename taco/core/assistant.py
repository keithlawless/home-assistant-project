"""Carries out fast-path intents and phrases the spoken reply.

Returns {"handled": bool, "speech": str, "silent": bool}. When a request is
ambiguous ("cancel the timer" with two running), Taco asks which one and
remembers the pending intent briefly, so a bare "pasta" in the follow-up
turn completes it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .intents import Intent, normalize, parse, say_clock, say_duration
from .timers import Timer

if TYPE_CHECKING:
    from .app import State

log = logging.getLogger(__name__)

PENDING_TTL_S = 20


def _reply(speech: str = "", silent: bool = False) -> dict[str, Any]:
    return {"handled": True, "speech": speech, "silent": silent}


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _names(timers: list[Timer]) -> str:
    labels = [t.label or "unnamed" for t in timers]
    return ", ".join(labels[:-1]) + f" and {labels[-1]}" if len(labels) > 1 else labels[0]


def _status(t: Timer) -> str:
    if t.state == "ringing":
        return f"{t.describe()} is done"
    left = say_duration(t.remaining())
    return f"{t.describe()} has {left} left" + (", paused" if t.state == "paused" else "")


class Assistant:
    def __init__(self, state: State):
        self.state = state
        self.pending: tuple[Intent, float] | None = None

    @property
    def timers(self):
        return self.state.db.timers

    async def handle(self, text: str) -> dict[str, Any]:
        intent = parse(text)
        if intent is None and self.pending and time.monotonic() < self.pending[1]:
            # A bare answer to "which timer?" -> retry the pending intent with it as the label.
            pending, _ = self.pending
            label = normalize(text).removeprefix("the ").removesuffix(" timer").strip()
            if label and len(label.split()) <= 3:
                intent = Intent(pending.name, {**pending.slots, "label": label})
        self.pending = None
        if intent is None:
            return {"handled": False}
        log.info("intent %s %s", intent.name, intent.slots)
        handler = getattr(self, f"_do_{intent.name}")
        result = await handler(**intent.slots)
        await self.state.broadcast_timers()
        return result

    def _ask(self, intent_name: str, slots: dict[str, Any], question: str) -> dict[str, Any]:
        self.pending = (Intent(intent_name, slots), time.monotonic() + PENDING_TTL_S)
        return _reply(question)

    def _pick(self, label: str | None, kind: str = "timer") -> tuple[Timer | None, list[Timer]]:
        """(the single matching timer, or None plus the candidates)."""
        matches = self.timers.find(label, kind)
        return (matches[0], matches) if len(matches) == 1 else (None, matches)

    # -- timers --------------------------------------------------------------

    async def _do_set_timer(self, seconds: float, label: str | None) -> dict[str, Any]:
        t = self.timers.add_timer(seconds, label)
        name = f"{t.label.capitalize()} timer" if t.label else "Timer"
        return _reply(f"{name} set for {say_duration(seconds)}.")

    async def _do_time_left(self, label: str | None) -> dict[str, Any]:
        timers = self.timers.find(label, "timer")
        if not timers:
            return _reply(f"I don't see a {label} timer." if label else "You don't have any timers running.")
        if len(timers) == 1:
            t = timers[0]
            if t.state == "ringing" or t.remaining() < 1:
                return _reply(f"{t.describe().capitalize()} is done.")
            return _reply(f"{say_duration(t.remaining())} left on {t.describe()}.")
        parts = [_status(t) for t in timers]
        return _reply((", ".join(parts[:-1]) + f", and {parts[-1]}.").capitalize())

    async def _do_list_timers(self) -> dict[str, Any]:
        everything = self.timers.all()
        if not everything:
            return _reply("You don't have any timers or alarms set.")
        now = datetime.now()
        parts = [_status(t) if t.kind == "timer"
                 else f"{t.describe()} is set for {say_clock(datetime.fromtimestamp(t.ends_at), now)}"
                 for t in everything]
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f", and {parts[-1]}"
        return _reply(text[0].upper() + text[1:] + ".")

    async def _do_cancel_timer(self, kind: str, label: str | None, all: bool) -> dict[str, Any]:
        noun = "alarm" if kind == "alarm" else "timer"
        matches = self.timers.find(label, kind)
        if not matches:
            if label:
                return _reply(f"I don't see a {label} {noun}.")
            return _reply(f"You don't have any {noun}s set.")
        if all or len(matches) == 1:
            self.timers.remove([t.id for t in matches])
            if len(matches) == 1:
                return _reply(f"Canceled {matches[0].describe()}.")
            return _reply(f"Canceled all {len(matches)} {noun}s.")
        return self._ask("cancel_timer", {"kind": kind, "all": False},
                         f"You have {len(matches)} {noun}s: {_names(matches)}. Which one?")

    async def _do_add_time(self, seconds: float, label: str | None) -> dict[str, Any]:
        t, matches = self._pick(label)
        if t is None:
            if not matches:
                return _reply("There's no timer to add time to." if not label else f"I don't see a {label} timer.")
            return self._ask("add_time", {"seconds": seconds}, f"Which timer, {_names(matches)}?")
        t = self.timers.add_time(t, seconds)
        return _reply(f"Added {say_duration(seconds)}. {_status(t).capitalize()}.")

    async def _do_pause_timer(self, label: str | None) -> dict[str, Any]:
        t, matches = self._pick(label)
        if t is None:
            if not matches:
                return _reply("There's no timer running.")
            return self._ask("pause_timer", {}, f"Which timer, {_names(matches)}?")
        t = self.timers.pause(t)
        return _reply(f"Paused {t.describe()} with {say_duration(t.remaining())} left.")

    async def _do_resume_timer(self, label: str | None) -> dict[str, Any]:
        paused = [t for t in self.timers.find(label, "timer") if t.state == "paused"]
        if not paused:
            return _reply("There's no paused timer.")
        if len(paused) > 1:
            return self._ask("resume_timer", {}, f"Which timer, {_names(paused)}?")
        t = self.timers.resume(paused[0])
        return _reply(f"Resumed {t.describe()}. {say_duration(t.remaining())} to go.")

    async def _do_stop(self) -> dict[str, Any]:
        self.timers.stop_ringing()
        await self.state.hub.broadcast({"type": "voice_command", "command": "stop"})
        return _reply(silent=True)

    # -- alarms and reminders -------------------------------------------------

    async def _do_set_alarm(self, at: datetime, label: str | None) -> dict[str, Any]:
        self.timers.add_alarm(at.timestamp(), label)
        return _reply(f"Alarm set for {say_clock(at, datetime.now())}.")

    async def _do_set_reminder(self, at: datetime, message: str) -> dict[str, Any]:
        self.timers.add_reminder(at.timestamp(), message)
        now = datetime.now()
        delta = (at - now).total_seconds()
        when = f"in {say_duration(delta)}" if delta < 3 * 3600 else f"at {say_clock(at, now)}"
        message = re.sub(r"\bmy\b", "your", message)
        return _reply(f"OK, I'll remind you to {message} {when}.")

    # -- quick info ----------------------------------------------------------

    async def _do_time(self) -> dict[str, Any]:
        return _reply(f"It's {datetime.now().strftime('%-I:%M %p')}.")

    async def _do_date(self) -> dict[str, Any]:
        now = datetime.now()
        return _reply(f"Today is {now.strftime('%A, %B')} {_ordinal(now.day)}.")

    async def _do_weather_now(self) -> dict[str, Any]:
        w = self.state.weather
        if not w:
            return {"handled": False}  # let Hermes try the web
        c, today = w["current"], w["today"]
        speech = (f"It's {c['temp']} degrees and {c['condition'].lower()} in {w['location'].split(',')[0]}. "
                  f"Today's high is {today['high']} with a low of {today['low']}")
        if today["precip"] and today["precip"] >= 30:
            speech += f", and a {today['precip']} percent chance of rain"
        return _reply(speech + ".")

    async def _do_volume(self, set: float | None = None, step: float | None = None) -> dict[str, Any]:
        if set is not None:
            level = set
        else:
            level = min(1.0, max(0.1, (await get_volume()) + (step or 0)))
        await run("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level:.2f}")
        await self.state.hub.broadcast({"type": "volume", "level": level})
        return _reply(f"Volume {round(level * 10)}.")


async def run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode()


async def get_volume() -> float:
    m = re.search(r"Volume:\s*([\d.]+)", await run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"))
    return float(m.group(1)) if m else 0.5
