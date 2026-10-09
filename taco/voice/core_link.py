"""taco-voice's connection to taco-core: the fast-path intent call, plus a
WebSocket subscription for things core decides (a timer went off, the
screen's stop button was tapped)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

log = logging.getLogger(__name__)


class CoreLink:
    def __init__(self, core_url: str, on_message: Callable[[dict[str, Any]], None]):
        self.url = core_url.rstrip("/")
        self._on_message = on_message
        self._http = httpx.Client(timeout=3.0)
        threading.Thread(target=self._listen, daemon=True, name="core-ws").start()

    def intent(self, text: str) -> dict[str, Any]:
        """Ask core's fast path; any failure means 'not handled' so Hermes gets it."""
        try:
            r = self._http.post(f"{self.url}/api/intent", json={"text": text})
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("intent call failed: %s", e)
            return {"handled": False}

    def stop_ringing(self) -> None:
        try:
            self._http.post(f"{self.url}/api/stop", json={"speech": False})
        except httpx.HTTPError as e:
            log.warning("stop call failed: %s", e)

    def _listen(self) -> None:
        ws_url = self.url.replace("http", "ws", 1) + "/ws"
        while True:
            try:
                with connect(ws_url, open_timeout=5) as ws:
                    log.info("connected to core")
                    for raw in ws:
                        try:
                            self._on_message(json.loads(raw))
                        except Exception:
                            log.exception("core message handler failed")
            except (OSError, WebSocketException) as e:
                log.debug("core websocket: %s", e)
            time.sleep(2)
