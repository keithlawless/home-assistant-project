"""Where music plays: rooms ("play jazz in the bedroom", "play music
everywhere") and Bluetooth speakers ("play on Sonos", "list speakers").

Radio and Spotify play into the taco_music loopback sink
(deploy/pipewire/60-taco-music.conf). Its output normally follows the default
sink, the kitchen speaker. Choosing a Bluetooth speaker connects it and points
the loopback's output at it; Taco's own voice stays in the kitchen.

Rooms: "kitchen" is that local output (the kitchen speaker, or the Bluetooth
speaker standing in for it); every other room is a satellite with a speaker,
which taco-voice feeds from the taco_music monitor while the room is in
`rooms`. Leaving the kitchen out mutes the loopback's output, not its input,
so the satellites still get the music.

The choice is temporary. Music goes back to the kitchen when asked, after
idle_revert_s with no music playing, when the speaker drops off, or when
taco-core restarts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .intents import normalize
from .media import find_node, run

log = logging.getLogger(__name__)

AUDIO_SINK_UUID = "0000110b"   # A2DP Audio Sink
LOOPBACK_OUT = "taco_music.out"
CHECK_S = 30
_GENERIC = {"the", "my", "a", "speaker", "speakers", "bluetooth", "on", "to", "in", "through", "over"}
_KITCHEN_WORDS = {"here", "kitchen", "back", "home", "this"}
KITCHEN_ROOM = "kitchen"
_HERE_WORDS = {"here", "back", "home", "this", "room", "in"}
_EVERYWHERE = re.compile(r"(?:in )?(?:everywhere|every (?:room|speaker)|all (?:the )?(?:rooms|speakers)"
                         r"|(?:all over |throughout )?the (?:whole )?house|the whole house|whole house)")


@dataclass(frozen=True)
class Speaker:
    name: str
    mac: str = ""            # "" = the kitchen speaker (the default sink)
    connected: bool = False

    @property
    def is_kitchen(self) -> bool:
        return not self.mac


KITCHEN = Speaker("the kitchen speaker")


def parse_devices(out: str) -> list[tuple[str, str]]:
    """`bluetoothctl devices` lines ("Device AA:BB:… Sonos Era 100") -> [(mac, name)]."""
    return [(m.group(1), m.group(2).strip())
            for m in re.finditer(r"^Device ([0-9A-F:]{17}) (.+)$", out, re.MULTILINE)]


def parse_info(out: str) -> dict[str, Any]:
    """The parts of `bluetoothctl info MAC` that matter here."""
    def field(name: str) -> str | None:
        m = re.search(rf"^\s*{name}: (.+)$", out, re.MULTILINE)
        return m.group(1).strip() if m else None
    return {
        "name": field("Alias") or field("Name"),
        "paired": field("Paired") == "yes",
        "connected": field("Connected") == "yes",
        "audio_sink": AUDIO_SINK_UUID in out.lower() or (field("Icon") or "").startswith("audio"),
    }


def _spoken_name(name: str) -> str:
    """'Sonos Move (SONOS 0C1E)' -> 'Sonos Move': the suffix is a serial number."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() or name


def resolve(spoken: str | None, speakers: list[Speaker]) -> Speaker | None:
    """'sonos' -> Sonos Era 100; 'here' / 'the kitchen' / None -> KITCHEN."""
    words = [w for w in normalize(spoken or "").split() if w not in _GENERIC]
    if not words or set(words) <= _KITCHEN_WORDS:
        return KITCHEN
    by_words = [(s, normalize(s.name).split()) for s in speakers]
    for test in (all, any):
        hits = [s for s, name in by_words if test(w in name for w in words)]
        if hits:
            return next((s for s in hits if s.connected), hits[0])
    return None


def resolve_rooms(spoken: str | None, here: str, rooms: list[str]) -> set[str] | None:
    """Which rooms a spoken place means, or None if it isn't one (it may be a
    Bluetooth speaker). None or "here" is the room the request came from;
    "everywhere" is every room; "the kitchen and the bedroom" is both."""
    text = normalize(spoken or "")
    if not text:
        return {here}
    if _EVERYWHERE.fullmatch(text):
        return set(rooms)
    out: set[str] = set()
    for part in re.split(r"\s+(?:and|plus)\s+|\s*,\s*", text):
        words = [w for w in part.split() if w not in _GENERIC]
        if not words or set(words) <= _HERE_WORDS:
            out.add(here)
            continue
        phrase = " ".join(words)
        match = next((r for r in rooms if normalize(r) == phrase), None)
        if match is None:
            return None
        out.add(match)
    return out


def describe_rooms(rooms: set[str], here: str, all_rooms: list[str]) -> str:
    """How to say where music plays: "here", "in the bedroom", "everywhere"."""
    if rooms == {here}:
        return "here"
    if len(all_rooms) > 1 and rooms >= set(all_rooms):
        return "everywhere"
    order = {r: i for i, r in enumerate(all_rooms)}
    names = [f"the {r}" for r in sorted(rooms, key=lambda r: (order.get(r, len(order)), r))]
    return "in " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def _sink_prefix(mac: str) -> str:
    return f"bluez_output.{mac.replace(':', '_')}"


class Speakers:
    def __init__(self, idle_revert_s: float, music_playing: Callable[[], bool],
                 on_change: Callable[[], Awaitable[None]], clock: Callable[[], float] = time.monotonic,
                 satellite_rooms: list[str] | None = None):
        self.idle_revert_s = idle_revert_s
        self._music_playing = music_playing
        self._on_change = on_change
        self._clock = clock
        self.current: Speaker | None = None   # None = the kitchen speaker
        # The rooms music plays in: the kitchen (the local output) and satellites.
        self.all_rooms = [KITCHEN_ROOM, *(satellite_rooms or [])]
        self.rooms: set[str] = {KITCHEN_ROOM}
        self._kitchen_muted = False
        self._idle_since: float | None = None
        self._lock = asyncio.Lock()

    @property
    def satellite_rooms(self) -> list[str]:
        """The satellites music plays on now (taco-voice feeds them)."""
        return [r for r in self.all_rooms if r in self.rooms and r != KITCHEN_ROOM]

    async def set_rooms(self, rooms: set[str]) -> None:
        """Play music in these rooms. Without the kitchen, a Bluetooth speaker
        standing in for it is let go too."""
        rooms = {r for r in rooms if r in self.all_rooms} or {KITCHEN_ROOM}
        if KITCHEN_ROOM not in rooms and self.current:
            await self.back("music moved to another room", notify=False)
        async with self._lock:
            changed = rooms != self.rooms
            self.rooms = rooms
            self._idle_since = None
            await self._mute_kitchen(KITCHEN_ROOM not in rooms)
        if changed:
            log.info("music plays in %s", ", ".join(sorted(rooms)))
            await self._on_change()

    async def list(self) -> list[Speaker]:
        """Paired Bluetooth audio speakers (not including the kitchen speaker)."""
        out = []
        for mac, name in parse_devices(await run("bluetoothctl", "devices", "Paired", timeout=10)):
            info = parse_info(await run("bluetoothctl", "info", mac, timeout=10))
            if info["audio_sink"]:
                out.append(Speaker(_spoken_name(info["name"] or name), mac, info["connected"]))
        return out

    async def select(self, speaker: Speaker) -> bool:
        """Send music to `speaker`. Returns False if it couldn't be reached."""
        if speaker.is_kitchen:
            await self.back("asked")
            return True
        async with self._lock:
            if self.current and self.current.mac == speaker.mac:
                return True
            await run("bluetoothctl", "connect", speaker.mac, timeout=20)
            sink = await self._wait_for_sink(speaker.mac)
            node = await find_node(lambda p: p.get("node.name") == LOOPBACK_OUT)
            if sink is None or node is None:
                log.warning("couldn't switch music to %s (%s)", speaker.name,
                            "no Bluetooth sink" if sink is None else "no taco_music loopback")
                await run("bluetoothctl", "disconnect", speaker.mac, timeout=10)
                return False
            await run("pw-metadata", str(node[0]), "target.object", sink, timeout=5)
            previous, self.current = self.current, speaker
            self.rooms = {KITCHEN_ROOM}   # it stands in for the kitchen speaker
            await self._mute_kitchen(False)
            self._idle_since = None
            log.info("music now plays on %s (%s)", speaker.name, sink)
            if previous:
                await run("bluetoothctl", "disconnect", previous.mac, timeout=10)
        await self._on_change()
        return True

    async def back(self, reason: str, notify: bool = True) -> None:
        """Music back to the kitchen speaker; let the Bluetooth speaker go."""
        async with self._lock:
            previous, self.current = self.current, None
            await self._clear_target()
            if previous:
                log.info("music back on the kitchen speaker (%s)", reason)
                await run("bluetoothctl", "disconnect", previous.mac, timeout=10)
        if previous and notify:
            await self._on_change()

    async def reset(self) -> None:
        """At startup: WirePlumber may have restored a Bluetooth target, and a
        speaker left connected stays on its Bluetooth input."""
        await self._clear_target()
        self.rooms = {KITCHEN_ROOM}
        await self._mute_kitchen(False, force=True)
        for speaker in await self.list():
            if speaker.connected:
                log.info("disconnecting %s left over from before the restart", speaker.name)
                await run("bluetoothctl", "disconnect", speaker.mac, timeout=10)

    async def volume_target(self) -> str:
        """What "turn it up" should change: the Bluetooth speaker while music
        plays on one, otherwise the default sink."""
        if self.current and (sink := await self._sink_node(self.current.mac)):
            return str(sink[0])
        return "@DEFAULT_AUDIO_SINK@"

    async def check(self) -> None:
        """Switch back if the speaker dropped off or music has been off a while."""
        if not self.current and self.rooms == {KITCHEN_ROOM}:
            return
        if self.current and await self._sink_node(self.current.mac) is None:
            await self.back(f"{self.current.name} disconnected")
            return
        if self._music_playing():
            self._idle_since = None
            return
        if self._idle_since is None:
            self._idle_since = self._clock()
        elif self._clock() - self._idle_since >= self.idle_revert_s:
            reason = f"no music for {self.idle_revert_s / 60:.0f} min"
            if self.current:
                await self.back(reason)
            if self.rooms != {KITCHEN_ROOM}:
                log.info("music rooms back to the kitchen (%s)", reason)
                await self.set_rooms({KITCHEN_ROOM})

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                await self.check()
            except Exception:
                log.exception("speaker check failed")

    async def pair(self, scan_s: int = 30) -> Speaker | None:
        """Find a speaker in pairing mode, then pair with and trust it. It is
        left disconnected until someone asks to play on it."""
        paired = {mac for mac, _ in parse_devices(await run("bluetoothctl", "devices", "Paired", timeout=10))}
        await run("bluetoothctl", "--timeout", str(scan_s), "scan", "on", timeout=scan_s + 10)
        for mac, name in parse_devices(await run("bluetoothctl", "devices", timeout=10)):
            if mac in paired:
                continue
            info = parse_info(await run("bluetoothctl", "info", mac, timeout=10))
            if not info["audio_sink"]:
                continue
            name = _spoken_name(info["name"] or name)
            log.info("pairing %s (%s)", name, mac)
            await run("bluetoothctl", "pair", mac, timeout=30)
            await run("bluetoothctl", "trust", mac, timeout=10)
            if parse_info(await run("bluetoothctl", "info", mac, timeout=10))["paired"]:
                await run("bluetoothctl", "disconnect", mac, timeout=10)
                return Speaker(name, mac)
            log.warning("pairing %s failed", mac)
        return None

    async def _wait_for_sink(self, mac: str, timeout_s: float = 10.0) -> str | None:
        end = self._clock() + timeout_s
        while True:
            if node := await self._sink_node(mac):
                return node[1]["node.name"]
            if self._clock() >= end:
                return None
            await asyncio.sleep(0.5)

    async def _sink_node(self, mac: str) -> tuple[int, dict[str, Any]] | None:
        prefix = _sink_prefix(mac)
        return await find_node(lambda p: p.get("media.class") == "Audio/Sink"
                               and p.get("node.name", "").startswith(prefix))

    async def _mute_kitchen(self, muted: bool, force: bool = False) -> None:
        """Silence the loopback's output (the kitchen, or a Bluetooth speaker
        standing in for it), keeping its input for the satellites."""
        if muted == self._kitchen_muted and not force:
            return
        if node := await find_node(lambda p: p.get("node.name") == LOOPBACK_OUT):
            await run("wpctl", "set-mute", str(node[0]), "1" if muted else "0", timeout=5)
            self._kitchen_muted = muted

    async def _clear_target(self) -> None:
        if node := await find_node(lambda p: p.get("node.name") == LOOPBACK_OUT):
            await run("pw-metadata", "-d", str(node[0]), "target.object", timeout=5)
