import asyncio

import httpx
import pytest

from taco.core.media import RADIO_BROWSERS, search_stations

STATION = {"name": "WGBH 89.7 ", "url_resolved": "https://example.org/wgbh", "tags": "news", "countrycode": "US"}


def search(handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await search_stations(http, "wgbh")
    return asyncio.run(go())


def test_falls_back_to_another_server_when_one_is_down():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(503) if len(hosts) == 1 else httpx.Response(200, json=[STATION])
    assert search(handler)[0]["name"] == "WGBH 89.7"
    assert hosts == [httpx.URL(u).host for u in RADIO_BROWSERS[:2]]


def test_gives_up_after_every_server_fails():
    with pytest.raises(httpx.HTTPStatusError):
        search(lambda request: httpx.Response(503))


def test_client_errors_are_not_retried():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(400)
    with pytest.raises(httpx.HTTPStatusError):
        search(handler)
    assert len(hosts) == 1


def test_mpv_never_takes_a_saved_stream_volume(monkeypatch, tmp_path):
    """WirePlumber's saved "Music" volume (0.008 after Spotify ducked) made
    radio nearly silent; mpv's stream opts out of restoring it."""
    import asyncio

    from taco.core import media

    seen = {}

    class Proc:
        returncode = None

        async def wait(self):
            await asyncio.sleep(3600)

    async def fake_exec(*args, **kw):
        seen["args"], seen["env"] = args, kw.get("env") or {}
        return Proc()

    async def on_change():
        pass

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", fake_exec)

    async def go():
        await media.RadioPlayer("taco_music", on_change=on_change).play({"name": "WGBH", "url": "http://x"})
    asyncio.run(go())
    assert "state.restore-props = false" in seen["env"]["PIPEWIRE_PROPS"]
    assert "--audio-device=pipewire/taco_music" in seen["args"]
