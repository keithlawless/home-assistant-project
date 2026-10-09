"""Matter devices through the local matter.js server (ws://127.0.0.1:5580/ws).

The server is the Matter controller: it commissions devices onto Taco's
fabric and keeps live subscriptions. This client mirrors its node list and
turns endpoints with an OnOff cluster into Taco devices ("node-endpoint").
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from .base import DeviceError, DeviceState, Found

log = logging.getLogger(__name__)

ONOFF, LEVEL, BASIC_INFO, DESCRIPTOR = 6, 8, 40, 29

# Matter device type ids -> Taco kind
DEVICE_TYPES = {
    0x0100: "light", 0x0101: "light", 0x010C: "light", 0x010D: "light",
    0x010A: "plug", 0x010B: "plug",
    0x0103: "switch", 0x0104: "dimmer", 0x0105: "dimmer",
    0x002B: "fan",
}


def _endpoints(node: dict[str, Any]) -> list[int]:
    """Endpoints (other than the root) that expose OnOff."""
    eps = {int(path.split("/")[0]) for path in node.get("attributes", {})
           if path.split("/")[1] == str(ONOFF)}
    return sorted(e for e in eps if e != 0)


def _found(node: dict[str, Any]) -> list[Found]:
    attrs = node.get("attributes", {})
    vendor = attrs.get(f"0/{BASIC_INFO}/1") or ""
    product = attrs.get(f"0/{BASIC_INFO}/3") or "Matter device"
    label = attrs.get(f"0/{BASIC_INFO}/5") or ""
    eps = _endpoints(node)
    out = []
    for ep in eps:
        types = [d.get("0") for d in attrs.get(f"{ep}/{DESCRIPTOR}/0") or [] if isinstance(d, dict)]
        kind = next((DEVICE_TYPES[t] for t in types if t in DEVICE_TYPES), "plug")
        caps = ["power"] + (["level"] if f"{ep}/{LEVEL}/0" in attrs else [])
        name = label or product
        if len(eps) > 1:
            name = f"{name} {eps.index(ep) + 1}"
        out.append(Found("matter", f"{node['node_id']}-{ep}", f"{node['node_id']}-{ep}",
                         name, f"{vendor} {product}".strip(), kind, caps))
    return out


def _split(address: str) -> tuple[int, int]:
    node, ep = address.split("-")
    return int(node), int(ep)


class MatterDriver:
    protocol = "matter"

    def __init__(self, url: str = "ws://127.0.0.1:5580/ws",
                 on_change: Callable[[], Awaitable[None]] | None = None):
        self.url = url
        self.nodes: dict[int, dict[str, Any]] = {}
        self.connected = False
        self._on_change = on_change
        self._ws = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}

    # -- connection -----------------------------------------------------------

    async def run(self) -> None:
        """Keep a connection open and mirror node state. Runs forever."""
        while True:
            try:
                async with websockets.connect(self.url, max_size=None) as ws:
                    self._ws = ws
                    await ws.recv()  # server info
                    reader = asyncio.create_task(self._read(ws))
                    nodes = await self.command("start_listening")
                    self.nodes = {n["node_id"]: n for n in nodes}
                    self.connected = True
                    log.info("matter server connected: %d nodes", len(self.nodes))
                    await self._changed()
                    await reader
            except (OSError, websockets.WebSocketException) as e:
                log.debug("matter server unavailable: %s", e)
            finally:
                self.connected = False
                self._ws = None
                for fut in self._pending.values():
                    fut.cancel()
                self._pending.clear()
            await asyncio.sleep(5)

    async def _read(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if "message_id" in msg and msg["message_id"] in self._pending:
                fut = self._pending.pop(msg["message_id"])
                if "error_code" in msg:
                    fut.set_exception(DeviceError(msg.get("details") or f"matter error {msg['error_code']}"))
                else:
                    fut.set_result(msg.get("result"))
            elif "event" in msg:
                await self._event(msg["event"], msg.get("data"))

    async def _event(self, event: str, data: Any) -> None:
        if event in ("node_added", "node_updated") and isinstance(data, dict):
            self.nodes[data["node_id"]] = data
        elif event == "node_removed":
            self.nodes.pop(data, None)
        elif event == "attribute_updated" and isinstance(data, list) and len(data) == 3:
            node_id, path, value = data
            if node_id in self.nodes:
                self.nodes[node_id].setdefault("attributes", {})[path] = value
        elif event == "node_event":
            return
        else:
            return
        await self._changed()

    async def _changed(self) -> None:
        if self._on_change:
            await self._on_change()

    async def command(self, command: str, timeout: float = 30, **args: Any) -> Any:
        if self._ws is None:
            raise DeviceError("Matter server isn't running")
        msg_id = str(next(self._ids))
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({"message_id": msg_id, "command": command, "args": args}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise DeviceError(f"Matter {command} timed out") from None

    # -- Driver interface -------------------------------------------------------

    async def discover(self) -> list[Found]:
        """Matter devices are 'found' once commissioned onto Taco's fabric."""
        return [f for node in self.nodes.values() for f in _found(node)]

    async def state(self, address: str) -> DeviceState:
        node_id, ep = _split(address)
        node = self.nodes.get(node_id)
        if not node:
            return DeviceState(online=False)
        attrs = node.get("attributes", {})
        level = attrs.get(f"{ep}/{LEVEL}/0")
        return DeviceState(
            online=bool(node.get("available")),
            on=attrs.get(f"{ep}/{ONOFF}/0"),
            level=round(level / 254 * 100) if isinstance(level, (int, float)) else None,
        )

    async def set_power(self, address: str, on: bool) -> DeviceState:
        node_id, ep = _split(address)
        await self.command("device_command", node_id=node_id, endpoint_id=ep, cluster_id=ONOFF,
                           command_name="On" if on else "Off", payload={})
        self.nodes.get(node_id, {}).setdefault("attributes", {})[f"{ep}/{ONOFF}/0"] = on
        return await self.state(address)

    async def set_level(self, address: str, level: int) -> DeviceState:
        node_id, ep = _split(address)
        raw = max(1, min(254, round(level / 100 * 254)))
        await self.command("device_command", node_id=node_id, endpoint_id=ep, cluster_id=LEVEL,
                           command_name="MoveToLevelWithOnOff",
                           payload={"level": raw, "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0})
        return await self.state(address)

    # -- commissioning ------------------------------------------------------------

    async def commission(self, code: str, network_only: bool,
                         wifi_ssid: str | None = None, wifi_password: str | None = None) -> list[Found]:
        """Add a device to Taco's fabric.

        network_only=True: the device is already on Wi-Fi (e.g. shared from
        Apple/Google/Alexa Home with a pairing code). False: a brand-new device
        in pairing mode, set up over Bluetooth with the house Wi-Fi credentials.
        """
        if not network_only:
            if not (wifi_ssid and wifi_password):
                raise DeviceError("Wi-Fi credentials are needed to set up a new device over Bluetooth")
            await self.command("set_wifi_credentials", ssid=wifi_ssid, credentials=wifi_password)
        node = await self.command("commission_with_code", timeout=300,
                                  code=code.replace("-", "").replace(" ", ""), network_only=network_only)
        self.nodes[node["node_id"]] = node
        return _found(node)

    async def remove(self, address: str) -> None:
        node_id, _ = _split(address)
        await self.command("remove_node", node_id=node_id)
        self.nodes.pop(node_id, None)
