import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from taco.core.spotify import SEARCH_LIMIT, Playable, SpotifyClient, SpotifyError

CFG = {"client_id": "cid", "client_secret": "secret", "refresh_token": "seed", "device_name": "Taco"}

ARTIST = {"name": "Miles Davis", "uri": "spotify:artist:miles"}
ALBUM = {"name": "Kind of Blue", "uri": "spotify:album:kob", "artists": [ARTIST]}
TRACK = {"name": "So What", "uri": "spotify:track:sowhat", "artists": [ARTIST], "album": ALBUM}
PLAYLIST = {"name": "Chill Dinner", "uri": "spotify:playlist:chill"}


class FakeSpotify:
    """The bits of the Spotify Web API Taco uses."""

    def __init__(self, devices=("Taco",), playlists=(), search=None):
        self.devices = list(devices)
        self.playlists = list(playlists)
        self.search_results = search or {}
        self.calls: list[tuple[str, str, dict, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "accounts.spotify.com":
            assert request.headers["authorization"] == "Basic " + base64.b64encode(b"cid:secret").decode()
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        path, params = url.path.removeprefix("/v1"), dict(url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, params, body))
        if path == "/me/player/devices":
            return httpx.Response(200, json={"devices": [{"id": f"id-{n}", "name": n} for n in self.devices]})
        if path == "/search":
            assert int(params["limit"]) <= SEARCH_LIMIT
            out = {}
            for t in params["type"].split(","):
                out[f"{t}s"] = {"items": self.search_results.get(t, [])}
            return httpx.Response(200, json=out)
        if path == "/me/playlists":
            return httpx.Response(200, json={"items": self.playlists, "next": None})
        if path == "/me":
            return httpx.Response(200, json={"id": "keith"})
        if path == "/me/player" and request.method == "GET":
            return httpx.Response(200, json={"device": {"name": "Taco"}, "is_playing": True, "item": TRACK})
        if path == "/me/player/shuffle":   # what Spotify really sent back, 2026-11-09
            return httpx.Response(200, text="7a6f2c", headers={"content-type": "text/plain"})
        return httpx.Response(204)


def run(fake, fn, restart=None):
    async def go(tmp):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
            return await fn(SpotifyClient(CFG, http, tmp, restart_player=restart))
    return go


@pytest.fixture
def go(tmp_path):
    return lambda fake, fn, restart=None: asyncio.run(run(fake, fn, restart)(tmp_path))


def test_plays_on_the_taco_device(go):
    fake = FakeSpotify(devices=("Phone", "Taco"))
    go(fake, lambda c: c.play(Playable("Kind of Blue", "album", context_uri="spotify:album:kob")))
    play = next(c for c in fake.calls if c[1] == "/me/player/play")
    assert play[2]["device_id"] == "id-Taco" and play[3] == {"context_uri": "spotify:album:kob"}
    shuffle = next(c for c in fake.calls if c[1] == "/me/player/shuffle")
    assert shuffle[2]["state"] == "false"


def test_a_track_plays_from_its_album(go):
    fake = FakeSpotify(search={"track": [TRACK]})

    async def fn(c):
        thing = await c.resolve("so what", by="miles davis")
        await c.play(thing)
        return thing
    thing = go(fake, fn)
    assert thing.name == "So What by Miles Davis"
    play = next(c for c in fake.calls if c[1] == "/me/player/play")
    assert play[3] == {"context_uri": "spotify:album:kob", "offset": {"uri": "spotify:track:sowhat"}}


def test_missing_device_restarts_librespot_once(go, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr("taco.core.spotify.asyncio.sleep", lambda s: real_sleep(0))
    fake = FakeSpotify(devices=())
    restarts = []

    async def restart():
        restarts.append(1)
        fake.devices = ["Taco"]
    assert go(fake, lambda c: c.taco_device(), restart) == "id-Taco"
    assert restarts == [1]


def test_device_still_missing_is_an_error(go, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr("taco.core.spotify.asyncio.sleep", lambda s: real_sleep(0))
    with pytest.raises(SpotifyError):
        go(FakeSpotify(devices=()), lambda c: c.taco_device(), restart=lambda: real_sleep(0))


def test_my_playlist_by_fuzzy_name(go):
    fake = FakeSpotify(playlists=[{"name": "Discover Weekly", "uri": "spotify:playlist:dw", "owner": {}},
                                  {"name": "Dinner Party", "uri": "spotify:playlist:dp", "owner": {}}])
    thing = go(fake, lambda c: c.resolve("discover weekly", playlist=True))
    assert thing.context_uri == "spotify:playlist:dw" and thing.name == "your Discover Weekly playlist"


def test_liked_songs_is_the_collection(go):
    thing = go(FakeSpotify(), lambda c: c.resolve("", kind="liked"))
    assert thing.context_uri == "spotify:user:keith:collection" and thing.shuffle


def test_album(go):
    fake = FakeSpotify(search={"album": [ALBUM]})
    thing = go(fake, lambda c: c.resolve("kind of blue", kind="album"))
    assert thing.context_uri == "spotify:album:kob" and thing.name == "Kind of Blue by Miles Davis"
    assert any(c[2].get("q") == "album:kind of blue" for c in fake.calls)


def test_unqualified_name_prefers_a_close_artist(go):
    fake = FakeSpotify(search={"artist": [ARTIST], "track": [TRACK]})
    thing = go(fake, lambda c: c.resolve("miles davis"))
    assert thing.kind == "artist" and thing.context_uri == "spotify:artist:miles" and thing.shuffle


def test_a_mood_finds_a_playlist(go):
    fake = FakeSpotify(search={"artist": [ARTIST], "playlist": [PLAYLIST]})
    thing = go(fake, lambda c: c.resolve("something chill for dinner"))
    assert thing.kind == "playlist" and thing.context_uri == "spotify:playlist:chill"


def test_nothing_found(go):
    assert go(FakeSpotify(), lambda c: c.resolve("zzzz")) is None


def test_now_playing_only_on_taco(go):
    song = go(FakeSpotify(), lambda c: c.now_playing())
    assert song == {"title": "So What", "artist": "Miles Davis", "album": "Kind of Blue", "playing": True}


# -- source order in the assistant: the NAS, then Spotify, then radio ----------------

class FakeApi:
    def __init__(self, thing):
        self.thing, self.asked = thing, []

    async def resolve(self, query, by=None, kind=None, playlist=False):
        self.asked.append(query)
        return self.thing


def _assistant(library_hit: bool, spotify_thing, monkeypatch):
    from taco.core import assistant as mod

    played = []
    library = SimpleNamespace(ready=True, resolve=lambda q, by, kind: SimpleNamespace(name="Lib") if library_hit else None,
                              mentions_artist=lambda q: False)

    async def play_library(plan):
        played.append(("library", plan.name))

    async def play_spotify(thing):
        played.append(("spotify", thing.name))

    async def play_station(st):
        played.append(("radio", st["name"]))

    async def stations(http, q):
        return [{"name": "Jazz FM"}]

    monkeypatch.setattr(mod, "search_stations", stations)
    state = SimpleNamespace(library=library, spotify_api=FakeApi(spotify_thing), play_library=play_library,
                            play_spotify=play_spotify, play_station=play_station, http=None,
                            media=SimpleNamespace(station=None))
    return mod.Assistant(state), played, state


@pytest.mark.parametrize("library_hit, spotify, radio, expect", [
    (True, Playable("Kind of Blue", "album"), False, "library"),
    (False, Playable("Kind of Blue", "album"), False, "spotify"),
    (False, None, False, "radio"),
    (False, Playable("Kind of Blue", "album"), True, "radio"),   # "... radio" never goes to Spotify
])
def test_source_order(monkeypatch, library_hit, spotify, radio, expect):
    a, played, state = _assistant(library_hit, spotify, monkeypatch)
    asyncio.run(a._do_media_play("kind of blue", radio=radio))
    assert played and played[0][0] == expect
    if radio:
        assert state.spotify_api.asked == []



def _assistant_for_empty_play(playing: bool, last: str, monkeypatch):
    from taco.core import assistant as mod

    calls = []
    sonos = SimpleNamespace(name="Sonos Move", is_kitchen=False)

    async def speakers_list():
        return [sonos]

    async def select(sp):
        calls.append(("speaker", sp.name))
        return True

    async def resume():
        calls.append(("resume",))
        return True

    async def play_station(st):
        calls.append(("radio", st))

    monkeypatch.setattr(mod, "resolve_speaker", lambda name, found: sonos)
    state = SimpleNamespace(
        music_playing=playing, last_source=last, spotify=SimpleNamespace(track={"name": "x"}),
        spotify_api=object(), resume_music=resume, play_station=play_station,
        speakers=SimpleNamespace(list=speakers_list, select=select),
        media=SimpleNamespace(station={"name": "Cheap Trick"}), library=SimpleNamespace(ready=True))
    return mod.Assistant(state), calls


def test_play_music_on_a_speaker_moves_what_is_playing(monkeypatch):
    a, calls = _assistant_for_empty_play(playing=True, last="spotify", monkeypatch=monkeypatch)
    r = asyncio.run(a._do_media_play("", speaker="sonos"))
    assert calls == [("speaker", "Sonos Move")] and r["speech"] == "Moved the music to Sonos Move."


def test_play_music_resumes_spotify_if_it_played_last(monkeypatch):
    a, calls = _assistant_for_empty_play(playing=False, last="spotify", monkeypatch=monkeypatch)
    r = asyncio.run(a._do_media_play("", speaker="sonos"))
    assert calls == [("speaker", "Sonos Move"), ("resume",)] and "Resuming Spotify" in r["speech"]
