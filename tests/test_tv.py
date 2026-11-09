import time
import json
from types import SimpleNamespace

from taco.voice.tv import TvPauser


class FakeAtv:
    def __init__(self, state="Playing"):
        self.state = state
        self.calls = []
        self.metadata = SimpleNamespace(playing=self._playing)
        self.remote_control = SimpleNamespace(pause=self._pause, play=self._play)

    async def _playing(self):
        return SimpleNamespace(device_state=SimpleNamespace(name=self.state))

    async def _pause(self):
        self.calls.append("pause")
        self.state = "Paused"

    async def _play(self):
        self.calls.append("play")
        self.state = "Playing"


def pauser(tmp_path, atv=None):
    p = TvPauser("192.0.2.1", ["living room"], tmp_path / "tv_paused.json")
    if atv:
        p._atv = atv
        p._connected.set()
    return p


def run(p, coro):
    return p._loop.run_until_complete(coro)


def test_pauses_a_playing_tv_and_resumes_it(tmp_path):
    atv = FakeAtv("Playing")
    p = pauser(tmp_path, atv)
    run(p, p._pause())
    assert atv.calls == ["pause"] and (tmp_path / "tv_paused.json").exists()
    run(p, p._pause())                     # a follow-up in the same turn
    assert atv.calls == ["pause"]
    run(p, p._resume())
    assert atv.calls == ["pause", "play"] and not (tmp_path / "tv_paused.json").exists()


def test_leaves_an_already_paused_tv_alone(tmp_path):
    atv = FakeAtv("Paused")
    p = pauser(tmp_path, atv)
    run(p, p._pause())
    run(p, p._resume())
    assert atv.calls == []


def test_doesnt_resume_if_someone_already_pressed_play(tmp_path):
    atv = FakeAtv("Playing")
    p = pauser(tmp_path, atv)
    run(p, p._pause())
    atv.state = "Playing"                  # remote pressed during the turn
    run(p, p._resume())
    assert atv.calls == ["pause"] and not p._paused_by_us


def test_only_its_rooms_pause(tmp_path):
    p = pauser(tmp_path, FakeAtv())
    sent = []
    p._loop = SimpleNamespace()            # turn_started must not schedule anything
    import taco.voice.tv as tv
    real = tv.asyncio.run_coroutine_threadsafe
    tv.asyncio.run_coroutine_threadsafe = lambda coro, loop: (sent.append(coro), coro.close())
    try:
        p.turn_started("kitchen")
        p.turn_started("living room")
    finally:
        tv.asyncio.run_coroutine_threadsafe = real
    assert len(sent) == 1


def test_unconnected_tv_is_skipped_quickly(tmp_path):
    p = pauser(tmp_path)                   # never connected
    t = time.monotonic()
    run(p, p._pause())
    assert time.monotonic() - t < 3 and not p._paused_by_us


def test_marker_age_decides_resume_after_restart(tmp_path):
    marker = tmp_path / "tv_paused.json"
    marker.write_text(json.dumps({"paused_at": time.time() - 30}))
    p = pauser(tmp_path)
    assert p._marker_age() < p.resume_within_s
    marker.write_text(json.dumps({"paused_at": time.time() - 3600}))
    assert p._marker_age() > p.resume_within_s


def test_resume_waits_out_idle_right_after_connecting(tmp_path):
    atv = FakeAtv("Playing")
    p = pauser(tmp_path, atv)
    run(p, p._pause())
    states = iter(["Idle", "Idle", "Paused"])      # a fresh connection's first answers
    real = atv._playing

    async def playing():
        r = await real()
        r.device_state.name = next(states, "Paused")
        return r
    atv.metadata.playing = playing
    run(p, p._resume())
    assert atv.calls == ["pause", "play"]
