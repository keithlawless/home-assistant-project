"""Spotify voice control through the Spotify Web API.

Taco is already a Spotify Connect speaker (librespot, "Taco"; see
deploy/systemd/taco-spotify.service), so playback goes there and through the
same taco_music path as the radio: Sonos routing and ducking just work. This
module finds things and tells Spotify to play them on that device.

It is written for a Development Mode app (2026 rules): search returns at most
10 results, artist top tracks and recommendations are gone, and playlist
contents are readable only for the user's own playlists. So Taco never reads
contents: it plays context URIs (artist, album, playlist) and Spotify picks
the tracks.

Setup (README, "Spotify"): a developer app with redirect
http://127.0.0.1:8888/callback, deploy/spotify-auth.py for the refresh token,
and librespot logged in once with --enable-device-auth, so "Taco" is always
listed as a device.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .oauth import OAuthToken

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_LIMIT = 10          # the Development Mode maximum
PLAYLISTS_TTL_S = 600


class SpotifyError(Exception):
    """Something to tell the user, e.g. the device isn't there."""


@dataclass
class Playable:
    """What to play: a context (artist, album, playlist), or tracks."""
    name: str                       # what Taco says: "Kind of Blue by Miles Davis"
    kind: str                       # artist, album, playlist, track, collection
    context_uri: str | None = None
    uris: list[str] = field(default_factory=list)
    offset_uri: str | None = None   # start the context at this track

    @property
    def shuffle(self) -> bool:
        return self.kind in ("artist", "collection")


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", re.sub(r"\b(the|a|an)\b", " ", s)).strip()


def _close(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _artists(item: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists") or [])


class SpotifyClient:
    def __init__(self, cfg: dict[str, Any], http: httpx.AsyncClient, store_dir: Path, restart_player=None):
        self._auth = OAuthToken("spotify", TOKEN_URL, cfg["client_id"], cfg["client_secret"],
                                cfg["refresh_token"], store_dir, basic_auth=True)
        self.http = http
        self.device_name = cfg.get("device_name", "Taco")
        self._restart_player = restart_player   # async; restarts librespot
        self._playlists: tuple[float, list[dict[str, Any]]] | None = None
        self._user_id: str | None = None

    # -- HTTP -----------------------------------------------------------------------

    async def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any] | None:
        r = await self._auth.request(self.http, method, f"{API}{path}", **kw)
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code == 404 and "player" in path:
            raise SpotifyError("no active device")
        if r.status_code == 403:
            raise SpotifyError("Spotify refused that (Premium, or not allowed for this app)")
        r.raise_for_status()
        return r.json()

    # -- devices and playback --------------------------------------------------------

    async def _find_device(self) -> str | None:
        body = await self._call("GET", "/me/player/devices") or {}
        for d in body.get("devices") or []:
            if d.get("name", "").lower() == self.device_name.lower():
                return d["id"]
        return None

    async def taco_device(self) -> str:
        """Taco's Connect device id. librespot can drop off the list (a
        network blip, an expired session): restart it once and look again."""
        if dev := await self._find_device():
            return dev
        if self._restart_player:
            log.info("spotify: %r isn't listed; restarting librespot", self.device_name)
            await self._restart_player()
            for _ in range(10):
                await asyncio.sleep(1)
                if dev := await self._find_device():
                    return dev
        raise SpotifyError(f"the {self.device_name} speaker isn't showing up in Spotify")

    async def play(self, thing: Playable) -> None:
        device = await self.taco_device()
        body: dict[str, Any] = {}
        if thing.context_uri:
            body["context_uri"] = thing.context_uri
            if thing.offset_uri:
                body["offset"] = {"uri": thing.offset_uri}
        else:
            body["uris"] = thing.uris
        await self._call("PUT", "/me/player/play", params={"device_id": device}, json=body)
        try:   # after play: before it the device may not be active yet
            await self._call("PUT", "/me/player/shuffle", params={"state": str(thing.shuffle).lower(),
                                                                  "device_id": device})
        except (SpotifyError, httpx.HTTPError) as e:
            log.info("spotify: shuffle not set: %s", e)

    async def pause(self) -> None:
        await self._call("PUT", "/me/player/pause")

    async def resume(self) -> None:
        await self._call("PUT", "/me/player/play", params={"device_id": await self.taco_device()})

    async def skip(self, step: int) -> None:
        await self._call("POST", "/me/player/next" if step > 0 else "/me/player/previous")

    async def now_playing(self) -> dict[str, str] | None:
        """The track on Taco, or None if nothing is, or it's on another device."""
        body = await self._call("GET", "/me/player")
        if not body or (body.get("device") or {}).get("name", "").lower() != self.device_name.lower():
            return None
        item = body.get("item") or {}
        if not item:
            return None
        return {"title": item.get("name", ""), "artist": _artists(item),
                "album": (item.get("album") or {}).get("name", ""), "playing": bool(body.get("is_playing"))}

    # -- finding things ----------------------------------------------------------------

    async def search(self, q: str, types: str) -> dict[str, list[dict[str, Any]]]:
        body = await self._call("GET", "/search", params={"q": q, "type": types, "limit": SEARCH_LIMIT}) or {}
        return {k: [i for i in (v.get("items") or []) if i] for k, v in body.items()}

    async def my_playlists(self) -> list[dict[str, Any]]:
        if self._playlists and time.monotonic() - self._playlists[0] < PLAYLISTS_TTL_S:
            return self._playlists[1]
        out: list[dict[str, Any]] = []
        url_params = {"limit": 50, "offset": 0}
        while True:
            body = await self._call("GET", "/me/playlists", params=url_params) or {}
            out += [{"name": p.get("name", ""), "uri": p["uri"], "owner": (p.get("owner") or {}).get("display_name")}
                    for p in body.get("items") or [] if p]
            if not body.get("next") or len(out) >= 500:
                break
            url_params["offset"] += 50
        self._playlists = (time.monotonic(), out)
        return out

    async def _user(self) -> str:
        if not self._user_id:
            self._user_id = (await self._call("GET", "/me") or {}).get("id", "")
        return self._user_id

    async def resolve(self, query: str, by: str | None = None, kind: str | None = None,
                      playlist: bool = False) -> Playable | None:
        """The best thing to play for a spoken request, or None."""
        query = query.strip()
        if playlist or kind == "playlist":
            return await self._my_playlist(query) or await self._public_playlist(query)
        if kind == "liked":
            return Playable("your liked songs", "collection", context_uri=f"spotify:user:{await self._user()}:collection")
        if kind == "album":
            q = f"album:{query}" + (f" artist:{by}" if by else "")
            albums = (await self.search(q, "album")).get("albums") or []
            if albums:
                a = albums[0]
                return Playable(f"{a['name']} by {_artists(a)}", "album", context_uri=a["uri"])
            return None
        if kind == "artist" and not by:
            return await self._artist(query)
        if by:
            tracks = (await self.search(f"track:{query} artist:{by}", "track")).get("tracks") or []
            if tracks:
                return self._track(tracks[0])
            # "Kind of Blue by Miles Davis" is an album, not a song.
            return await self.resolve(query, by, kind="album") if kind is None else None
        # Unqualified: an artist if one matches the name closely, else the best
        # track, else a playlist ("something chill").
        found = await self.search(query, "artist,track,playlist")
        artists, tracks = found.get("artists") or [], found.get("tracks") or []
        if artists and _close(artists[0]["name"], query) >= 0.85:
            a = artists[0]
            return Playable(a["name"], "artist", context_uri=a["uri"])
        if tracks and _close(tracks[0]["name"], query) >= 0.85:
            return self._track(tracks[0])
        if playlists := found.get("playlists"):
            p = playlists[0]
            return Playable(f"the {p['name']} playlist", "playlist", context_uri=p["uri"])
        return self._track(tracks[0]) if tracks else None

    async def _artist(self, query: str) -> Playable | None:
        artists = (await self.search(query, "artist")).get("artists") or []
        if artists:
            return Playable(artists[0]["name"], "artist", context_uri=artists[0]["uri"])
        return None

    def _track(self, t: dict[str, Any]) -> Playable:
        """A song, played from its album so the music carries on afterwards."""
        album = t.get("album") or {}
        name = f"{t['name']} by {_artists(t)}"
        if album.get("uri"):
            return Playable(name, "track", context_uri=album["uri"], offset_uri=t["uri"])
        return Playable(name, "track", uris=[t["uri"]])

    async def _my_playlist(self, query: str) -> Playable | None:
        mine = await self.my_playlists()
        if not mine:
            return None
        best = max(mine, key=lambda p: _close(p["name"], query))
        if _close(best["name"], query) >= 0.6:
            return Playable(f"your {best['name']} playlist", "playlist", context_uri=best["uri"])
        return None

    async def _public_playlist(self, query: str) -> Playable | None:
        found = (await self.search(query, "playlist")).get("playlists") or []
        if found:
            return Playable(f"the {found[0]['name']} playlist", "playlist", context_uri=found[0]["uri"])
        return None
