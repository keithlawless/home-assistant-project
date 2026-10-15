"""Pi health: CPU, memory, temperature and throttling, as notification cards.

Reads /proc and /sys directly (no psutil) and `vcgencmd get_throttled`.
CPU is averaged over a window because Whisper briefly maxes out every core
on each request. Throttle flags are OR-ed over the same window so a card
doesn't flicker. Each condition clears a few points below where it was raised.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STAT = Path("/proc/stat")
MEMINFO = Path("/proc/meminfo")
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")

# `vcgencmd get_throttled` bits that describe *now*; bits 16-19 are the same
# flags latched since boot, which would keep a card up forever.
UNDER_VOLTAGE = 0x1
THROTTLE_NOW = 0x2 | 0x4 | 0x8   # frequency capped, throttled, soft temperature limit

CONDITIONS = ("cpu", "memory", "temperature", "power", "throttle")


def parse_cpu(stat: str) -> tuple[int, int]:
    """(busy, total) jiffies from the aggregate "cpu" line of /proc/stat."""
    fields = [int(x) for x in stat.split("\n", 1)[0].split()[1:9]]
    idle = fields[3] + fields[4]          # idle + iowait
    total = sum(fields)
    return total - idle, total


def parse_memory(meminfo: str) -> float:
    """Percent of RAM in use (what MemAvailable says can't be reclaimed)."""
    values = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        values[name] = int(rest.split()[0]) if rest.split() else 0
    total = values["MemTotal"]
    return 100.0 * (total - values["MemAvailable"]) / total


def parse_throttled(output: str) -> int:
    """`throttled=0x50005` -> 0x50005."""
    return int(output.strip().split("=", 1)[1], 16)


@dataclass
class Limits:
    cpu_percent: float = 75.0
    memory_percent: float = 75.0
    temp_c: float = 75.0       # Pi 5 soft-throttles at 80 °C, hard at 85 °C
    clear_margin: float = 5.0


def evaluate(readings: dict[str, Any], active: set[str], limits: Limits) -> tuple[set[str], set[str]]:
    """Which conditions start and which end. A reading of None (unavailable,
    or the CPU window not yet full) leaves that condition as it is."""
    over = {"cpu": (readings.get("cpu_percent"), limits.cpu_percent),
            "memory": (readings.get("memory_percent"), limits.memory_percent),
            "temperature": (readings.get("temp_c"), limits.temp_c)}
    start, end = set(), set()
    for name, (value, limit) in over.items():
        if value is None:
            continue
        if name not in active and value >= limit:
            start.add(name)
        elif name in active and value < limit - limits.clear_margin:
            end.add(name)
    flags = readings.get("throttled")
    if flags is not None:
        for name, mask in (("power", UNDER_VOLTAGE), ("throttle", THROTTLE_NOW)):
            if flags & mask and name not in active:
                start.add(name)
            elif not flags & mask and name in active:
                end.add(name)
    return start, end


def card(name: str, readings: dict[str, Any]) -> tuple[str, str]:
    """(title, body) for a condition that just started."""
    if name == "cpu":
        return f"Taco's CPU is busy: {readings['cpu_percent']:.0f}%", "Averaged over the last 2 minutes."
    if name == "memory":
        return f"Taco is low on memory: {readings['memory_percent']:.0f}% used", ""
    if name == "temperature":
        return f"Taco is running hot: {readings['temp_c']:.0f} °C", "Check that the Pi's fan and vents are clear."
    if name == "power":
        return "Taco's power supply is too weak", "The Pi reports under-voltage. Use the official 27 W USB-C supply."
    return "Taco is slowing itself down", "The Pi is throttling its CPU to protect itself (heat or power)."


@dataclass
class HealthMonitor:
    limits: Limits = field(default_factory=Limits)
    window_s: float = 120.0
    interval_s: float = 10.0
    active: set[str] = field(default_factory=set)
    _cpu: deque = field(default_factory=deque)       # (time, busy, total)
    _flags: deque = field(default_factory=deque)     # (time, throttled bits)
    latest: dict[str, Any] = field(default_factory=dict)

    async def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        readings: dict[str, Any] = {"cpu_percent": None, "memory_percent": None, "temp_c": None,
                                    "throttled": None}
        try:
            busy, total = parse_cpu(STAT.read_text())
            self._cpu.append((now, busy, total))
            while now - self._cpu[0][0] > self.window_s + self.interval_s / 2:
                self._cpu.popleft()
            t0, b0, n0 = self._cpu[0]
            if now - t0 >= self.window_s - self.interval_s / 2 and total > n0:
                readings["cpu_percent"] = 100.0 * (busy - b0) / (total - n0)
        except (OSError, ValueError, IndexError) as e:
            log.debug("cpu read failed: %s", e)
        try:
            readings["memory_percent"] = parse_memory(MEMINFO.read_text())
        except (OSError, ValueError, KeyError, ZeroDivisionError) as e:
            log.debug("memory read failed: %s", e)
        try:
            readings["temp_c"] = int(THERMAL.read_text()) / 1000
        except (OSError, ValueError) as e:
            log.debug("temperature read failed: %s", e)
        if (bits := await _vcgencmd_throttled()) is not None:
            self._flags.append((now, bits))
            while now - self._flags[0][0] > self.window_s:
                self._flags.popleft()
            combined = 0
            for _, b in self._flags:
                combined |= b
            readings["throttled"] = combined
        self.latest = {**readings, "active": sorted(self.active)}
        return readings


async def _vcgencmd_throttled() -> int | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "vcgencmd", "get_throttled", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
        return parse_throttled(out.decode())
    except (OSError, ValueError, IndexError, asyncio.TimeoutError):
        return None
