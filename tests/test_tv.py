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


class FakeDirecTv(FakeAtv):
    """Measured on DirecTV: "play" toggles, and a pause ad reports as Playing.
    Over the ad, play returns to the paused program."""

    def __init__(self, state="Playing"):
        super().__init__(state)
        self.ad = False

    async def _play(self):
        self.calls.append("play")
        if self.ad:
            self.ad, self.state = False, "Paused"
        else:
            self.state = "Paused" if self.state == "Playing" else "Playing"

    def start_pause_ad(self):
        self.ad, self.state = True, "Playing"


def pauser_at(tmp_path, atv, paused_ago, playing_ago):
    """A pauser that paused paused_ago s back, and saw Playing playing_ago s back."""
    p = pauser(tmp_path, atv)
    p._paused_by_us = True
    now = time.monotonic()
    p._paused_at = now - paused_ago
    p._playing_since = None if playing_ago is None else now - playing_ago
    return p


def test_resumes_past_a_pause_ad(tmp_path):
    atv = FakeDirecTv("Paused")
    atv.start_pause_ad()                                      # 25 s into a 40 s turn
    p = pauser_at(tmp_path, atv, paused_ago=40, playing_ago=15)
    run(p, p._resume())
    assert atv.calls == ["play", "play"] and atv.state == "Playing" and not atv.ad


def test_leaves_alone_a_tv_someone_resumed_early(tmp_path):
    atv = FakeDirecTv("Playing")                              # play pressed 5 s into the turn
    p = pauser_at(tmp_path, atv, paused_ago=30, playing_ago=25)
    run(p, p._resume())
    assert atv.calls == [] and atv.state == "Playing"


def test_still_paused_takes_one_play(tmp_path):
    atv = FakeDirecTv("Paused")
    p = pauser_at(tmp_path, atv, paused_ago=10, playing_ago=None)
    run(p, p._resume())
    assert atv.calls == ["play"] and atv.state == "Playing"


def test_push_updates_time_the_switch_to_playing(tmp_path):
    p = pauser(tmp_path)
    p._on_state("Paused")
    assert p._playing_since is None
    p._on_state("Playing")
    first = p._playing_since
    p._on_state("Playing")                                    # repeats don't move it
    assert p._playing_since == first


def test_listeners_survive_garbage_collection(tmp_path, monkeypatch):
    """pyatv keeps listeners by weak reference; ours must stay alive."""
    import gc
    import weakref
    import pyatv
    import pyatv.storage.file_storage as fs

    class Weakly:   # like pyatv's StateProducer: only a weak reference to the listener
        _ref = None

        @property
        def listener(self):
            return self._ref and self._ref()

        @listener.setter
        def listener(self, target):
            self._ref = weakref.ref(target)

    class PushUpdater(Weakly):
        def start(self):
            pass

    class Conn(FakeAtv, Weakly):
        def __init__(self):
            super().__init__()
            self.push_updater = PushUpdater()

    conn = Conn()

    class Storage:
        async def load(self):
            pass

    async def scan(*a, **k):
        return [object()]

    async def connect(*a, **k):
        return conn

    monkeypatch.setattr(fs.FileStorage, "default_storage", staticmethod(lambda loop: Storage()))
    monkeypatch.setattr(pyatv, "scan", scan)
    monkeypatch.setattr(pyatv, "connect", connect)
    p = pauser(tmp_path)
    run(p, p._connect())
    gc.collect()
    assert conn.listener is not None and conn.push_updater.listener is not None
    conn.push_updater.listener.playstatus_update(None, SimpleNamespace(device_state=SimpleNamespace(name="Playing")))
    assert p._last_state == "Playing"


def test_a_reconnect_mid_turn_doesnt_resume(tmp_path, monkeypatch):
    """Only a restart (marker at startup) resumes on connect; a dropped
    connection during a turn must leave the TV paused until the turn ends."""
    atv = FakeDirecTv("Paused")
    p = pauser(tmp_path)
    p._paused_by_us = True            # mid-turn, no marker from a restart
    connects = []

    async def fake_connect():
        connects.append(1)
        p._atv = atv
        p._connected.set()
        p._lost = __import__("asyncio").Event()
        if len(connects) == 2:
            raise SystemExit          # stop the loop after one reconnect
        p._lost.set()                 # the connection drops at once

    monkeypatch.setattr(p, "_connect", fake_connect)
    monkeypatch.setattr("taco.voice.tv.RECONNECT_S", (0,))
    try:
        run(p, p._run())
    except SystemExit:
        pass
    assert len(connects) == 2 and atv.calls == []
