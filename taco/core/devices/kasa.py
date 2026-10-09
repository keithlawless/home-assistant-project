"""TP-Link Kasa plugs and switches (HS103, HS105, and kin) over the LAN.

No cloud: python-kasa talks to the devices directly. Discovery broadcasts to
the LAN's broadcast address (192.168.7.255 on this /22, not .255 of a /24).
"""

from __future__ import annotations

import asyncio
import json
import logging

from kasa import Device, Discover, KasaException, Module

from .base import DeviceError, DeviceState, Found

log = logging.getLogger(__name__)

TIMEOUT_S = 5


async def lan_broadcast() -> str:
    """Broadcast address of the first non-loopback IPv4 interface."""
    proc = await asyncio.create_subprocess_exec("ip", "-j", "-4", "addr", stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    for iface in json.loads(out or b"[]"):
        for addr in iface.get("addr_info", []):
            if iface.get("ifname") != "lo" and addr.get("broadcast"):
                return addr["broadcast"]
    return "255.255.255.255"


def _kind(dev: Device) -> tuple[str, list[str]]:
    if Module.Light in dev.modules:
        light = dev.modules[Module.Light]
        if getattr(light, "is_dimmable", False):
            return ("dimmer" if dev.device_type.name in ("WallSwitch", "Dimmer") else "light"), ["power", "level"]
        return "light", ["power"]
    if dev.device_type.name == "WallSwitch":
        return "switch", ["power"]
    return "plug", ["power"]


def _state(dev: Device) -> DeviceState:
    level = None
    if Module.Light in dev.modules and getattr(dev.modules[Module.Light], "is_dimmable", False):
        level = dev.modules[Module.Light].brightness
    return DeviceState(online=True, on=dev.is_on, level=level)


class KasaDriver:
    protocol = "kasa"

    def __init__(self, username: str | None = None, password: str | None = None):
        # Only newer firmware (KLAP) needs TP-Link account credentials.
        self._creds = {"username": username, "password": password} if username else {}
        self._devices: dict[str, Device] = {}   # by host
        self._lock = asyncio.Lock()

    async def discover(self) -> list[Found]:
        target = await lan_broadcast()
        found = await Discover.discover(target=target, discovery_timeout=3, **self._creds)
        out = []
        for host, dev in found.items():
            try:
                await dev.update()
            except KasaException as e:
                log.warning("kasa %s: %s", host, e)
                continue
            self._devices[host] = dev
            kind, caps = _kind(dev)
            out.append(Found("kasa", dev.mac.lower(), host, dev.alias or dev.model, dev.model, kind, caps))
        log.info("kasa discovery on %s: %d devices", target, len(out))
        return out

    async def _device(self, host: str) -> Device:
        if host not in self._devices:
            try:
                self._devices[host] = await asyncio.wait_for(
                    Discover.discover_single(host, **self._creds), TIMEOUT_S)
            except (KasaException, asyncio.TimeoutError, OSError) as e:
                raise DeviceError(f"{host} not reachable: {e}") from e
        return self._devices[host]

    async def _run(self, host: str, action) -> DeviceState:
        async with self._lock:
            try:
                dev = await self._device(host)
                if action:
                    await asyncio.wait_for(action(dev), TIMEOUT_S)
                await asyncio.wait_for(dev.update(), TIMEOUT_S)
                return _state(dev)
            except (KasaException, asyncio.TimeoutError, OSError) as e:
                self._devices.pop(host, None)  # reconnect next time (IP may have changed)
                raise DeviceError(f"{host}: {e}") from e

    async def state(self, address: str) -> DeviceState:
        return await self._run(address, None)

    async def set_power(self, address: str, on: bool) -> DeviceState:
        return await self._run(address, lambda d: d.turn_on() if on else d.turn_off())

    async def set_level(self, address: str, level: int) -> DeviceState:
        async def go(dev: Device):
            if Module.Light not in dev.modules:
                raise DeviceError("not dimmable")
            await dev.modules[Module.Light].set_brightness(max(1, min(100, level)))
        return await self._run(address, go)
