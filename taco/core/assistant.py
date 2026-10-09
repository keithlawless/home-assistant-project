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

from .devices.match import resolve
from .lists import canonical, split_items
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


def _join(items: list[str]) -> str:
    """['a', 'b', 'c'] -> 'a, b and c'."""
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else items[0]


def _names(timers: list[Timer]) -> str:
    return _join([t.label or "unnamed" for t in timers])


def _sentence(text: str) -> str:
    return text[0].upper() + text[1:]


def _status(t: Timer) -> str:
    if t.state == "ringing":
        return f"{t.describe()} is done"
    left = say_duration(t.remaining())
    return f"{t.describe()} has {left} left" + (", paused" if t.state == "paused" else "")


class Assistant:
    def __init__(self, state: State):
        self.state = state
        # (intent awaiting an answer, expiry, which slot the answer fills)
        self.pending: tuple[Intent, float, str] | None = None

    @property
    def timers(self):
        return self.state.db.timers

    async def handle(self, text: str) -> dict[str, Any]:
        intent = parse(text)
        if intent is None and self.pending and time.monotonic() < self.pending[1]:
            # A bare answer to "which one?" -> retry the pending intent with it.
            pending, _, key = self.pending
            answer = normalize(text).removeprefix("the ").removesuffix(" timer").removesuffix(" one").strip()
            if answer and len(answer.split()) <= 4:
                intent = Intent(pending.name, {**pending.slots, key: answer})
        self.pending = None
        if intent is None and self.state.scenes.get(text):
            intent = Intent("scene", {"name": text})   # "good night", "movie time"
        if intent is None:
            return {"handled": False}
        log.info("intent %s %s", intent.name, intent.slots)
        handler = getattr(self, f"_do_{intent.name}")
        result = await handler(**intent.slots)
        await self.state.broadcast_timers()
        return result

    def _ask(self, intent_name: str, slots: dict[str, Any], question: str, key: str = "label") -> dict[str, Any]:
        self.pending = (Intent(intent_name, slots), time.monotonic() + PENDING_TTL_S, key)
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

    # -- devices -----------------------------------------------------------------

    def _targets(self, intent_name: str, slots: dict[str, Any], target: str,
                 candidates: list[int] | None) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """Resolve a spoken target to devices, or produce the reply to give instead."""
        devices = self.state.devices.registry.all()
        if not devices:
            return None, _reply("I don't have any devices set up yet. Say find new devices to look for them.")
        pool = [d for d in devices if d["id"] in candidates] if candidates else devices
        if candidates and normalize(target) in ("both", "all", "all of them", "both of them", "all 3"):
            return pool, None
        m = resolve(target, pool, self.state.db.settings()["rooms"])
        if m is None:
            if candidates:
                return None, _reply("Sorry, I didn't catch which one.")
            return None, {"handled": False}  # maybe Hermes knows what they meant
        if m.scope == "room" and not m.ids:
            return None, _reply(f"There aren't any devices in the {m.room.lower()} yet.")
        chosen = [d for d in pool if d["id"] in m.ids]
        if m.ambiguous:
            names = [d["name"] for d in chosen]
            question = f"Which one, {', '.join(names[:-1])} or {names[-1]}?"
            return None, self._ask(intent_name, {**slots, "candidates": m.ids}, question, key="target")
        return chosen, None

    @staticmethod
    def _group_name(devices: list[dict[str, Any]]) -> str:
        rooms = {d["room"] for d in devices}
        if len(devices) == 1:
            return devices[0]["name"]
        if len(rooms) == 1 and None not in rooms:
            return f"the {next(iter(rooms)).lower()}"
        return "everything"

    @staticmethod
    def _failures(failed: list[dict[str, Any]]) -> str:
        if not failed:
            return ""
        return f" {_join([d['name'] for d in failed])} {'is' if len(failed) == 1 else 'are'}n't responding."

    async def _do_power(self, on: bool, target: str, candidates: list[int] | None = None) -> dict[str, Any]:
        devices, reply = self._targets("power", {"on": on}, target, candidates)
        if reply is not None:
            return reply
        ok, failed = await self.state.devices.control([d["id"] for d in devices], "power", on)
        word = "on" if on else "off"
        if not ok:
            return _reply(self._failures(failed).strip())
        name = self._group_name(ok)
        speech = f"Everything's {word}." if name == "everything" else f"{_sentence(name)} is {word}."
        return _reply(speech + self._failures(failed))

    async def _do_level(self, target: str, level: int, candidates: list[int] | None = None) -> dict[str, Any]:
        devices, reply = self._targets("level", {"level": level}, target, candidates)
        if reply is not None:
            return reply
        dimmable = [d for d in devices if "level" in d["capabilities"]]
        if not dimmable:
            return _reply(f"{_sentence(self._group_name(devices))} can only turn on and off, not dim.")
        ok, failed = await self.state.devices.control([d["id"] for d in dimmable], "level", level)
        if not ok:
            return _reply(self._failures(failed).strip())
        return _reply(f"{_sentence(self._group_name(ok))} set to {level} percent." + self._failures(failed))

    async def _do_device_status(self, target: str, candidates: list[int] | None = None) -> dict[str, Any]:
        devices, reply = self._targets("device_status", {}, target, candidates)
        if reply is not None:
            return reply
        await self.state.devices.refresh()
        parts = []
        for d in devices:
            st = self.state.devices.states.get(d["id"])
            if not st or not st.online:
                parts.append(f"{d['name']} isn't responding")
            else:
                level = f" at {st.level} percent" if st.on and st.level is not None else ""
                parts.append(f"{d['name']} is {'on' if st.on else 'off'}{level}")
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f", and {parts[-1]}"
        return _reply(text + ".")

    # -- lists -----------------------------------------------------------------

    @staticmethod
    def _list_name(name: str | None) -> str:
        n = canonical(name)
        return "to-do list" if n == "to do" else f"{n} list"

    async def _do_list_add(self, list: str | None, items: str) -> dict[str, Any]:
        wanted = split_items(items)
        added = self.state.lists.add(list, wanted)
        await self.state.broadcast_lists()
        where = self._list_name(list)
        if not added:
            return _reply(f"{_sentence(_join(wanted))} {'is' if len(wanted) == 1 else 'are'} already on your {where}.")
        return _reply(f"Added {_join(added)} to your {where}.")

    async def _do_list_remove(self, list: str | None, items: str) -> dict[str, Any]:
        wanted = split_items(items)
        removed = self.state.lists.remove(list, wanted)
        await self.state.broadcast_lists()
        if not removed:
            return _reply(f"I don't see {_join(wanted)} on your {self._list_name(list)}.")
        return _reply(f"Removed {_join(removed)}.")

    async def _do_list_read(self, list: str | None) -> dict[str, Any]:
        items = [i["text"] for i in self.state.lists.items(list) if not i["done"]]
        where = self._list_name(list)
        if not items:
            return _reply(f"Your {where} is empty.")
        if len(items) > 10:
            shown = ", ".join(items[:10])
            return _reply(f"You have {len(items)} things on your {where}. The first ten are {shown}.")
        noun = "thing" if len(items) == 1 else "things"
        return _reply(f"You have {len(items)} {noun} on your {where}: {_join(items)}.")

    async def _do_list_clear(self, list: str | None) -> dict[str, Any]:
        self.state.lists.clear(list)
        await self.state.broadcast_lists()
        return _reply(f"Cleared your {self._list_name(list)}.")

    # -- scenes, briefing, microphone ------------------------------------------

    async def _do_scene(self, name: str) -> dict[str, Any]:
        scene = self.state.scenes.get(name)
        if scene is None:
            return {"handled": False}   # Hermes can offer to create it
        speech = await self.state.run_scene(scene)
        return _reply(speech or "OK.")

    async def _do_scene_save(self, name: str) -> dict[str, Any]:
        actions = []
        for d in self.state.devices.payload():
            st = d["state"]
            if st["online"] is False or st["on"] is None:
                continue
            action: dict[str, Any] = {"type": "devices", "ids": [d["id"]], "on": bool(st["on"])}
            if st["on"] and st["level"] is not None:
                action["level"] = st["level"]
            actions.append(action)
        if not actions:
            return _reply("I couldn't read any device states to save.")
        self.state.scenes.save(name, actions)
        return _reply(f"Saved. Say {name} any time to set things up like this again.")

    async def _do_briefing(self) -> dict[str, Any]:
        return _reply(self.briefing())

    def briefing(self) -> str:
        now = datetime.now()
        part = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
        lines = [f"Good {part}! It's {now.strftime('%-I:%M %p')} on {now.strftime('%A')}."]
        if w := self.state.weather:
            c, today = w["current"], w["today"]
            line = (f"It's {c['temp']} degrees and {c['condition'].lower()}, "
                    f"with a high of {today['high']} and a low of {today['low']}")
            if today["precip"] and today["precip"] >= 30:
                line += f", and a {today['precip']} percent chance of rain"
            lines.append(line + ".")
        for alert in getattr(self.state, "alerts", [])[:2]:
            lines.append(f"Weather alert: {alert['event']}.")
        upcoming = [t for t in self.timers.all() if t.kind in ("alarm", "reminder") and t.state == "running"
                    and datetime.fromtimestamp(t.ends_at).date() == now.date()]
        for t in upcoming[:3]:
            when = datetime.fromtimestamp(t.ends_at).strftime("%-I:%M %p")
            lines.append(f"Reminder at {when}: {t.message}." if t.kind == "reminder" else f"Your alarm is set for {when}.")
        shopping = [i for i in self.state.lists.items("shopping") if not i["done"]]
        if shopping:
            lines.append(f"You have {len(shopping)} {'thing' if len(shopping) == 1 else 'things'} on your shopping list.")
        return " ".join(lines)

    async def _do_mic_mute(self) -> dict[str, Any]:
        self.state.db.update_settings({"mic_muted": True})
        await self.state.hub.broadcast({"type": "settings", "settings": self.state.db.settings()})
        return _reply("OK, I've stopped listening. Tap the microphone on the screen when you want me back.")

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
