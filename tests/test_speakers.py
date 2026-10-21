import asyncio

import pytest

from taco.core import speakers as sp
from taco.core.speakers import KITCHEN, Speaker, Speakers, parse_devices, parse_info, resolve

MAC = "AA:BB:CC:DD:EE:01"
SONOS = Speaker("Sonos Era 100", MAC)
INFO = f"""Device {MAC} (public)
	Name: Sonos Era 100
	Alias: Sonos Era 100
	Icon: audio-card
	Paired: yes
	Connected: no
	UUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)
"""
PHONE_INFO = INFO.replace("audio-card", "phone").replace("0000110b", "00001112")


def test_serial_suffix_dropped_from_names():
    assert sp._spoken_name("Sonos Move (SONOS 0C1E)") == "Sonos Move"
    assert sp._spoken_name("JBL Flip 6") == "JBL Flip 6"


def test_parse_devices_and_info():
    out = f"Device {MAC} Sonos Era 100\nDevice 11:22:33:44:55:66 Pixel 9\nsome noise\n"
    assert parse_devices(out) == [(MAC, "Sonos Era 100"), ("11:22:33:44:55:66", "Pixel 9")]
    info = parse_info(INFO)
    assert info == {"name": "Sonos Era 100", "paired": True, "connected": False, "audio_sink": True}
    assert parse_info(PHONE_INFO)["audio_sink"] is False


@pytest.mark.parametrize("spoken,expected", [
    ("sonos", SONOS), ("the sonos speaker", SONOS), ("era", SONOS),
    (None, KITCHEN), ("here", KITCHEN), ("the kitchen", KITCHEN), ("kitchen speaker", KITCHEN),
    ("garage", None),
])
def test_resolve(spoken, expected):
    assert resolve(spoken, [SONOS, Speaker("JBL Flip", "11:22:33:44:55:66")]) == expected


class Fake:
    """Stands in for bluetoothctl, pw-dump and pw-metadata."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, ...]] = []
        self.nodes = [{"node.name": "taco_music.out"}]
        self.connect_works = True
        monkeypatch.setattr(sp, "run", self.run)
        monkeypatch.setattr(sp, "find_node", self.find_node)

    async def run(self, *cmd, timeout=None):
        self.calls.append(cmd)
        if cmd[:2] == ("bluetoothctl", "connect") and self.connect_works:
            self.nodes.append({"node.name": f"bluez_output.{MAC.replace(':', '_')}.1",
                               "media.class": "Audio/Sink"})
        if cmd[:2] == ("bluetoothctl", "devices"):
            return f"Device {MAC} Sonos Era 100\n"
        if cmd[:2] == ("bluetoothctl", "info"):
            return INFO
        return ""

    async def find_node(self, match):
        for i, props in enumerate(self.nodes):
            if match(props):
                return i, props
        return None

    def drop_speaker(self):
        self.nodes = [n for n in self.nodes if not n["node.name"].startswith("bluez")]

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


def make(clock=None, playing=lambda: False):
    changes = []

    async def on_change():
        changes.append(1)
    s = Speakers(1800, music_playing=playing, on_change=on_change, clock=clock or (lambda: 0.0))
    return s, changes


def test_list_keeps_audio_speakers(monkeypatch):
    Fake(monkeypatch)
    s, _ = make()
    assert asyncio.run(s.list()) == [SONOS]


def test_select_points_loopback_at_speaker_then_back(monkeypatch):
    fake = Fake(monkeypatch)
    s, changes = make()

    async def go():
        assert await s.select(SONOS)
        assert s.current == SONOS
        assert fake.ran("pw-metadata", "0", "target.object", f"bluez_output.{MAC.replace(':', '_')}.1")
        assert await s.volume_target() == "1"   # the Bluetooth sink's node
        await s.select(KITCHEN)
        assert s.current is None
        assert fake.ran("pw-metadata", "-d", "0", "target.object")
        assert fake.ran("bluetoothctl", "disconnect", MAC)
        assert await s.volume_target() == "@DEFAULT_AUDIO_SINK@"
    asyncio.run(go())
    assert len(changes) == 2


def test_select_fails_when_speaker_unreachable(monkeypatch):
    fake = Fake(monkeypatch)
    fake.connect_works = False
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    now = [0.0]

    def clock():
        now[0] += 1
        return now[0]
    s, _ = make(clock=clock)
    assert asyncio.run(s.select(SONOS)) is False
    assert s.current is None
    assert not fake.ran("pw-metadata", "0", "target.object")


_real_sleep = asyncio.sleep


async def _no_sleep(_):
    await _real_sleep(0)


def test_reset_clears_target_and_disconnects(monkeypatch):
    fake = Fake(monkeypatch)
    real_run = fake.run

    async def run(*cmd, timeout=None):
        out = await real_run(*cmd, timeout=timeout)
        return out.replace("Connected: no", "Connected: yes")
    monkeypatch.setattr(sp, "run", run)
    s, _ = make()
    asyncio.run(s.reset())
    assert fake.ran("pw-metadata", "-d", "0", "target.object")
    assert fake.ran("bluetoothctl", "disconnect", MAC)


def test_switches_back_after_idle(monkeypatch):
    Fake(monkeypatch)
    now = [0.0]
    playing = [True]
    s, _ = make(clock=lambda: now[0], playing=lambda: playing[0])

    async def go():
        await s.select(SONOS)
        await s.check()
        assert s.current == SONOS          # music playing: stays
        playing[0] = False
        await s.check()                    # idle clock starts
        now[0] = 1799
        await s.check()
        assert s.current == SONOS
        now[0] = 1801
        await s.check()
        assert s.current is None
    asyncio.run(go())


def test_switches_back_when_speaker_drops(monkeypatch):
    fake = Fake(monkeypatch)
    s, _ = make(playing=lambda: True)

    async def go():
        await s.select(SONOS)
        fake.drop_speaker()
        await s.check()
        assert s.current is None
        assert fake.ran("pw-metadata", "-d", "0", "target.object")
    asyncio.run(go())


def test_pair_skips_known_and_non_audio(monkeypatch):
    fake = Fake(monkeypatch)
    new = "11:22:33:44:55:66"
    phone = "77:88:99:AA:BB:CC"
    paired = set()

    async def run(*cmd, timeout=None):
        fake.calls.append(cmd)
        if cmd == ("bluetoothctl", "devices", "Paired"):
            return "".join(f"Device {m} x\n" for m in paired)
        if cmd == ("bluetoothctl", "devices"):
            return f"Device {phone} Pixel\nDevice {new} JBL Flip 6\n"
        if cmd[:2] == ("bluetoothctl", "info"):
            info = PHONE_INFO if cmd[2] == phone else INFO.replace("Sonos Era 100", "JBL Flip 6")
            return info.replace("Paired: yes", "Paired: yes" if cmd[2] in paired else "Paired: no")
        if cmd[:2] == ("bluetoothctl", "pair"):
            paired.add(cmd[2])
        return ""
    monkeypatch.setattr(sp, "run", run)
    s, _ = make()
    found = asyncio.run(s.pair(scan_s=1))
    assert found == Speaker("JBL Flip 6", new)
    assert fake.ran("bluetoothctl", "trust", new) and not fake.ran("bluetoothctl", "pair", phone)
