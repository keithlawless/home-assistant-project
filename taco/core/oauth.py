"""OAuth 2.0 tokens for outside services (refresh-token grant).

One OAuthToken per service. The client ID, client secret and a seed refresh
token come from /srv/taco/config.toml. Services such as Netatmo issue a new
refresh token on every refresh and retire the old one, so the live tokens are
kept in a private file (<store_dir>/<name>.json, mode 0600) and the config's
refresh token is only used to start, or to start over if the stored one is
rejected.

Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

REFRESH_EARLY_S = 300   # refresh this long before the access token expires


class OAuthError(Exception):
    """The service refused every refresh token we have; a person must re-authorize."""


class OAuthToken:
    def __init__(self, name: str, token_url: str, client_id: str, client_secret: str,
                 seed_refresh_token: str, store_dir: Path):
        if not (client_id and client_secret and seed_refresh_token):
            raise ValueError(f"{name}: client_id, client_secret and refresh_token are all required")
        self.name = name
        self._token_url = token_url
        self._client = {"client_id": client_id, "client_secret": client_secret}
        self._seed = seed_refresh_token
        self._path = Path(store_dir) / f"{name}.json"
        self._lock = asyncio.Lock()
        self._tok: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            tok = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        # A new seed in config.toml (re-authorized by hand) wins over the store.
        return tok if tok.get("seed") == self._seed else {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._tok, f)
        os.replace(tmp, self._path)

    async def _refresh_with(self, http: httpx.AsyncClient, refresh_token: str) -> bool:
        r = await http.post(self._token_url, data={"grant_type": "refresh_token",
                                                   "refresh_token": refresh_token, **self._client})
        if r.status_code in (400, 401, 403):
            log.warning("%s: refresh token rejected (%s)", self.name, _error_code(r))
            return False
        r.raise_for_status()
        body = r.json()
        self._tok = {
            "seed": self._seed,
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token", refresh_token),
            "expires_at": time.time() + float(body.get("expires_in", 3600)),
        }
        self._save()
        log.info("%s: token refreshed, expires in %d min", self.name, float(body.get("expires_in", 3600)) // 60)
        return True

    async def _refresh(self, http: httpx.AsyncClient) -> None:
        stored = self._tok.get("refresh_token")
        if stored and await self._refresh_with(http, stored):
            return
        if stored != self._seed and await self._refresh_with(http, self._seed):
            return
        self._tok = {}
        raise OAuthError(f"{self.name}: authorization expired; put a new refresh_token in "
                         f"/srv/taco/config.toml and restart taco-core")

    async def access_token(self, http: httpx.AsyncClient, force: bool = False) -> str:
        async with self._lock:
            if force or not self._tok.get("access_token") \
                    or time.time() > self._tok.get("expires_at", 0) - REFRESH_EARLY_S:
                await self._refresh(http)
            return self._tok["access_token"]

    async def request(self, http: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
        """Send a request with the bearer token; on a 401/403, refresh once and retry."""
        token = await self.access_token(http)
        r = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code in (401, 403):
            stale = token
            async with self._lock:   # another caller may have refreshed already
                if self._tok.get("access_token") == stale:
                    self._tok["expires_at"] = 0
            token = await self.access_token(http)
            r = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        return r


def _error_code(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", r.status_code))
    except ValueError:
        return str(r.status_code)
