"""WebSocket fan-out to the kiosk UI (and any other open screens)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class Hub:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket, snapshot: dict[str, Any]) -> None:
        await ws.accept()
        self._clients.add(ws)
        await ws.send_json({"type": "snapshot", **snapshot})

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        clients = list(self._clients)
        if not clients:
            return
        results = await asyncio.gather(
            *(ws.send_json(message) for ws in clients), return_exceptions=True
        )
        for ws, result in zip(clients, results):
            if isinstance(result, Exception):
                self._clients.discard(ws)
