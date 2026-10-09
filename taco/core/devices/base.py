"""Common shapes for device drivers (Kasa, Matter)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Found:
    """A device seen on the network, registered or not."""
    protocol: str            # "kasa" | "matter"
    native_id: str           # stable id: Kasa MAC, Matter "node-endpoint"
    address: str             # how to reach it now: IP for Kasa, node id for Matter
    name: str                # the device's own name (Kasa alias, Matter label/product)
    model: str
    kind: str                # plug | light | dimmer | switch
    capabilities: list[str] = field(default_factory=lambda: ["power"])  # + "level"


@dataclass
class DeviceState:
    online: bool
    on: bool | None = None
    level: int | None = None  # 0-100


class Driver(Protocol):
    protocol: str

    async def discover(self) -> list[Found]: ...
    async def state(self, address: str) -> DeviceState: ...
    async def set_power(self, address: str, on: bool) -> DeviceState: ...
    async def set_level(self, address: str, level: int) -> DeviceState: ...


class DeviceError(Exception):
    """A device didn't respond or rejected a command."""
