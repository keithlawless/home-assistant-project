import queue
import socket
import threading
import time

import numpy as np
import pytest

from taco.voice.audio import FRAME_SAMPLES, NetworkMicrophone

FRAME = np.arange(FRAME_SAMPLES, dtype=np.int16)


def start_mic(**kw) -> NetworkMicrophone:
    mic = NetworkMicrophone("living room", 0, "s3cret", host="127.0.0.1", **kw)
    mic.start()
    return mic


def connect(mic, token="s3cret") -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", mic.port))
    sock.sendall(f"TACO living_room {token}\n".encode())
    return sock


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_frames_arrive_and_set_ready():
    ready = threading.Event()
    mic = start_mic(ready=ready)
    with connect(mic) as sock:
        sock.sendall(FRAME.tobytes() * 2 + b"\0" * 100)   # 2 frames + a partial one
        assert np.array_equal(mic.read(timeout=2), FRAME)
        assert np.array_equal(mic.read(timeout=2), FRAME)
        assert ready.is_set()
        with pytest.raises(queue.Empty):
            mic.read(timeout=0.2)


def test_bad_token_rejected():
    mic = start_mic()
    with connect(mic, token="wrong") as sock:
        sock.sendall(FRAME.tobytes())
        assert sock.recv(1) == b""   # server hung up
    assert mic.read_nowait() is None and not mic.connected


def test_disallowed_ip_rejected():
    mic = start_mic(allow=["192.168.4.0/24"])
    with connect(mic) as sock:
        assert sock.recv(1) == b""
    assert not mic.connected


def test_reconnect_replaces_old_stream():
    mic = start_mic()
    first = connect(mic)
    wait_for(lambda: mic.connected)
    old = mic._conn
    with connect(mic) as second:
        wait_for(lambda: mic._conn is not old and mic.connected)
        assert first.recv(1) == b""   # old stream closed
        second.sendall(FRAME.tobytes())
        assert np.array_equal(mic.read(timeout=2), FRAME)
    first.close()
    wait_for(lambda: not mic.connected)


def test_requires_token():
    with pytest.raises(ValueError):
        NetworkMicrophone("x", 0, "")


def test_record_request_from_satellite():
    # The request recorder reads mic.sample_rate; a satellite wake once crashed taco-voice here.
    from taco.voice.listen import record_request
    mic = start_mic()
    with connect(mic) as sock:
        sock.sendall(np.zeros(FRAME_SAMPLES * 4, dtype=np.int16).tobytes())
        assert record_request(mic, start_timeout_s=0.1, end_silence_s=0.5, max_seconds=2) is None


def test_stall_ignored_while_disconnected_and_reset_on_connect(caplog):
    mic = start_mic()
    mic.last_frame = time.monotonic() - 60
    mic.check_stall()
    assert not [r for r in caplog.records if "no audio" in r.message]
    with connect(mic):
        wait_for(lambda: mic.connected)
        mic.check_stall()   # last_frame was reset on connect
        assert not [r for r in caplog.records if "no audio" in r.message]
        mic.check_stall(now=time.monotonic() + 10)
        assert [r for r in caplog.records if "no audio" in r.message]


def fake_image(version: str, body: bytes = b"\x55" * 1000) -> bytes:
    """Enough of an ESP-IDF app image for image_version()."""
    head = bytearray(80)
    head[0] = 0xE9
    head[32:36] = (0xABCD5432).to_bytes(4, "little")
    head[48:48 + len(version)] = version.encode()
    return bytes(head) + body


def ask_update(mic, running, token="s3cret") -> bytes:
    with socket.create_connection(("127.0.0.1", mic.port)) as sock:
        sock.sendall(f"TACO-OTA living_room {token} {running}\n".encode())
        sock.settimeout(2)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
        return data


def test_image_version():
    from taco.voice.audio import image_version
    assert image_version(fake_image("20261105-0900-abc1234")) == "20261105-0900-abc1234"
    assert image_version(b"not an image" * 10) is None
    assert image_version(b"") is None


def test_update_sent_when_version_differs(tmp_path):
    import hashlib
    image = fake_image("v2")
    (tmp_path / "fw.bin").write_bytes(image)
    mic = start_mic(firmware=tmp_path / "fw.bin")
    reply = ask_update(mic, "v1")
    header, body = reply.split(b"\n", 1)
    assert header.decode() == f"IMAGE {len(image)} {hashlib.sha256(image).hexdigest()} v2"
    assert body == image
    assert not mic.connected   # an update check isn't the audio stream


def test_no_update_when_current_or_missing(tmp_path):
    (tmp_path / "fw.bin").write_bytes(fake_image("v2"))
    assert ask_update(start_mic(firmware=tmp_path / "fw.bin"), "v2") == b"NONE\n"
    assert ask_update(start_mic(firmware=tmp_path / "missing.bin"), "v1") == b"NONE\n"
    assert ask_update(start_mic(), "v1") == b"NONE\n"


def test_update_needs_the_token(tmp_path):
    (tmp_path / "fw.bin").write_bytes(fake_image("v2"))
    assert ask_update(start_mic(firmware=tmp_path / "fw.bin"), "v1", token="wrong") == b""


def test_update_check_leaves_audio_connection_alone(tmp_path):
    (tmp_path / "fw.bin").write_bytes(fake_image("v2"))
    mic = start_mic(firmware=tmp_path / "fw.bin")
    with connect(mic) as sock:
        wait_for(lambda: mic.connected)
        ask_update(mic, "v1")
        sock.sendall(FRAME.tobytes())
        assert np.array_equal(mic.read(timeout=2), FRAME)


def read_lines(sock, n, timeout=2.0):
    sock.settimeout(timeout)
    data = b""
    while data.count(b"\n") < n:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data.decode().splitlines()


def test_control_lines_reach_the_satellite():
    mic = start_mic()
    with connect(mic) as sock:
        wait_for(lambda: mic.connected)
        mic.send_line("STATE listening here")
        mic.send_line("MUTE on")
        assert read_lines(sock, 2) == ["STATE listening here", "MUTE on"]


def test_control_lines_dropped_without_a_satellite():
    mic = start_mic()
    mic.send_line("STATE idle")   # nobody connected: no error, nothing queued
    with connect(mic) as sock:
        wait_for(lambda: mic.connected)
        mic.send_line("MUTE off")
        assert read_lines(sock, 1) == ["MUTE off"]


def test_on_connect_runs_for_each_connection():
    seen = []
    mic = start_mic(on_connect=lambda m: (seen.append(m), m.send_line("STATE idle")))
    with connect(mic) as sock:
        assert read_lines(sock, 1) == ["STATE idle"]
    with connect(mic) as sock:
        assert read_lines(sock, 1) == ["STATE idle"]
    assert seen == [mic, mic]


def test_new_firmware_image_asks_satellite_to_update(tmp_path):
    import os
    fw = tmp_path / "fw.bin"
    fw.write_bytes(fake_image("v1"))
    mic = start_mic(firmware=fw)
    with connect(mic) as sock:
        wait_for(lambda: mic.connected)
        mic.check_firmware()            # unchanged since start: nothing sent
        fw.write_bytes(fake_image("v2"))
        os.utime(fw, (time.time() + 5, time.time() + 5))
        mic.check_firmware()
        mic.check_firmware()            # only once per new image
        mic.send_line("END")
        assert read_lines(sock, 2) == ["UPDATE", "END"]


class FakeSat:
    name = "living room"

    def __init__(self):
        self.lines = []
        self.second = object()   # its front end's second channel

    def send_line(self, line):
        self.lines.append(line)


def _loop_with_satellite():
    from taco.voice.main import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)   # no models or devices
    sat = FakeSat()
    loop.listeners = [(type("Mic", (), {"name": "kitchen"})(), None), (sat, None)]
    loop.active = loop.listeners[0]
    loop.state, loop.muted = "idle", False
    loop.events = type("E", (), {"send": lambda self, *a, **k: None})()
    loop._satellites = lambda: [sat]
    loop.music = type("M", (), {"reconnected": lambda self, name: None})()
    return loop, sat


def test_state_lines_say_whose_turn_it_is():
    loop, sat = _loop_with_satellite()
    loop._set_state("listening")
    loop.active = loop.listeners[1]
    loop._set_state("thinking")
    loop._set_state("idle")
    assert sat.lines == ["STATE listening elsewhere", "STATE thinking here", "STATE idle"]


def test_reconnecting_satellite_gets_mute_and_state():
    loop, sat = _loop_with_satellite()
    loop.muted, loop.state = True, "speaking"
    loop._sync_satellite(sat)
    assert sat.lines == ["MUTE on", "STATE speaking elsewhere"]


def send_event(mic, words, token="s3cret") -> bytes:
    with socket.create_connection(("127.0.0.1", mic.port)) as sock:
        sock.sendall(f"TACO-EVENT living_room {token} {words}\n".encode())
        sock.settimeout(2)
        data = b""
        while chunk := sock.recv(64):
            data += chunk
        return data


def test_mute_button_events(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="taco.voice.audio")
    mic = start_mic()
    assert mic.button_muted is None
    assert send_event(mic, "MUTE on") == b"OK\n"
    assert mic.button_muted is True
    assert "mute button on" in caplog.text
    assert send_event(mic, "MUTE off") == b"OK\n"
    assert mic.button_muted is False
    assert not mic.connected   # an event isn't the audio stream


def test_events_need_the_token():
    mic = start_mic()
    assert send_event(mic, "MUTE on", token="wrong") == b""
    assert mic.button_muted is None


def test_levels_event_is_logged(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="taco.voice.audio")
    mic = start_mic()
    assert send_event(mic, "LEVELS -70/-50 -48/-20 -90/-90 -47/-19") == b"OK\n"
    assert "channel levels (rms/peak dBFS) -70/-50 -48/-20 -90/-90 -47/-19" in caplog.text


# -- the speech front end: two channels, switched by a file --------------------

def connect_with(mic, extra: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", mic.port))
    sock.sendall(f"TACO living_room s3cret {extra}\n".encode())
    return sock


def test_two_channel_stream_splits_into_second_source():
    mic = start_mic()
    a, b = FRAME, (FRAME[::-1]).copy()
    both = np.empty(FRAME_SAMPLES * 2, dtype=np.int16)
    both[0::2], both[1::2] = a, b
    with connect_with(mic, "afe=on ch=2") as sock:
        sock.sendall(both.tobytes())
        assert np.array_equal(mic.read(timeout=2), a)
        assert np.array_equal(mic.second.read(timeout=2), b)
        assert mic.afe == "on" and mic.channels == 2
        assert mic.second.live and mic.second.name == "living room 2"


def test_mono_stream_leaves_second_channel_idle():
    mic = start_mic()
    with connect_with(mic, "afe=off ch=1") as sock:
        sock.sendall(FRAME.tobytes())
        assert np.array_equal(mic.read(timeout=2), FRAME)
        assert mic.afe == "off" and not mic.second.live
        assert mic.second.read_nowait() is None


def sent_lines(mic) -> list[str]:
    lines = []
    while not mic._out.empty():
        lines.append(mic._out.get_nowait())
    return lines


def test_afe_file_switches_the_board(tmp_path):
    afe = tmp_path / "living_room.afe"
    mic = NetworkMicrophone("living room", 0, "s3cret", host="127.0.0.1", afe_file=afe)
    mic._conn = object()   # connected, as far as check_afe cares
    mic._send_lines = lambda: None
    mic.afe = "off"
    mic.check_afe()
    assert sent_lines(mic) == []          # no file: leave the board as it is
    afe.write_text("on\n")
    mic.check_afe()
    assert sent_lines(mic) == ["AFE on"]
    mic.check_afe()
    assert sent_lines(mic) == []          # once; the next handshake reports the result


@pytest.mark.parametrize("reported", [None, "failed"])
def test_afe_never_asked_of_old_or_failed_boards(tmp_path, reported):
    afe = tmp_path / "living_room.afe"
    afe.write_text("on")
    mic = NetworkMicrophone("living room", 0, "s3cret", host="127.0.0.1", afe_file=afe)
    mic._conn = object()
    mic.afe = reported
    mic.check_afe()
    assert sent_lines(mic) == []          # no restart loop


def test_record_request_from_second_channel():
    # The request comes from whichever channel fired; this crashed taco-voice once.
    from taco.voice.listen import record_request
    mic = start_mic()
    both = np.zeros(FRAME_SAMPLES * 2 * 4, dtype=np.int16)
    with connect_with(mic, "afe=on ch=2") as sock:
        sock.sendall(both.tobytes())
        assert mic.second.sample_rate == 16000
        assert record_request(mic.second, start_timeout_s=0.1, end_silence_s=0.5, max_seconds=2) is None


def test_request_mix_adds_both_channels():
    from taco.voice.audio import SatelliteMix
    mic = start_mic()
    a = np.full(FRAME_SAMPLES, 100, dtype=np.int16)
    b = np.full(FRAME_SAMPLES, 30000, dtype=np.int16)
    both = np.empty(FRAME_SAMPLES * 2, dtype=np.int16)
    both[0::2], both[1::2] = a, b
    with connect_with(mic, "afe=on ch=2") as sock:
        sock.sendall(both.tobytes())
        mixed = SatelliteMix(mic).read(timeout=2)
        assert mixed.dtype == np.int16 and int(mixed[0]) == 30100


# -- replies on the satellite's own speaker --------------------------------------

def test_handshake_reports_a_speaker():
    mic = start_mic()
    with connect_with(mic, "afe=off ch=1 spk=1") as sock:
        sock.sendall(FRAME.tobytes())
        mic.read(timeout=2)
        assert mic.speaker
        assert mic.speaker_rate == 16000
    with connect_with(mic, "afe=on ch=1 spk=48000") as sock:   # 48 kHz firmware
        sock.sendall(FRAME.tobytes())
        wait_for(lambda: mic.afe == "on")
        assert mic.speaker and mic.speaker_rate == 48000
    with connect_with(mic, "afe=off ch=1 spk=0") as sock:
        sock.sendall(FRAME.tobytes())
        wait_for(lambda: mic.afe == "off")
        assert not mic.speaker
    with connect_with(mic, "afe=off ch=1") as sock:   # older firmware
        sock.sendall(FRAME.tobytes())
        wait_for(lambda: not mic.speaker)


def test_audio_is_framed_in_order_with_control_lines():
    mic = start_mic()
    with connect_with(mic, "afe=off ch=1 spk=1") as sock:
        sock.sendall(FRAME.tobytes())
        mic.read(timeout=2)
        mic.send_line("STATE speaking here")
        assert mic.send_audio(b"\x01\x02\x03\x04")
        assert mic.send_audio(b"\x05\x06", kind=b"MUSIC")
        mic.send_line("STOP")
        sock.settimeout(2)
        got = b""
        while not got.endswith(b"STOP\n"):
            got += sock.recv(4096)
        assert got == b"STATE speaking here\nAUDIO 4\n\x01\x02\x03\x04MUSIC 2\n\x05\x06STOP\n"


def test_resample_to_16k():
    from taco.voice.audio import resample
    tone = (np.sin(np.arange(22050) * 2 * np.pi * 440 / 22050) * 10000).astype(np.int16)
    out = resample(tone, 22050)
    assert out.dtype == np.int16 and len(out) == 16000
    assert abs(int(np.abs(out).max()) - 10000) < 200
    up = resample(tone, 22050, 48000)   # replies to a 48 kHz speaker
    assert len(up) == 48000 and abs(int(np.abs(up[100:-100]).max()) - 10000) < 300


class FakeSatForSpeaker:
    name = "living room"

    def __init__(self):
        self.audio, self.lines, self.t0 = [], [], time.monotonic()

    def send_audio(self, pcm):
        self.audio.append((time.monotonic() - self.t0, len(pcm)))
        return True

    def send_line(self, line):
        self.lines.append(line)


def test_satellite_speaker_paces_audio_and_waits_for_it():
    from taco.voice.audio import SatelliteSpeaker
    sat = FakeSatForSpeaker()
    spk = SatelliteSpeaker(sat)
    spk.play(np.zeros(16000, dtype=np.int16), 16000)   # 1 s
    t = time.monotonic()
    spk.wait()
    waited = time.monotonic() - t
    assert sum(n for _, n in sat.audio) == 32000
    assert 0.9 < waited < 1.6                     # played out, plus the board's latency
    assert sat.audio[-1][0] > 0.4                 # paced, not all sent at once


def test_satellite_speaker_stop_tells_the_board():
    from taco.voice.audio import SatelliteSpeaker
    sat = FakeSatForSpeaker()
    spk = SatelliteSpeaker(sat)
    spk.play(np.zeros(16000 * 3, dtype=np.int16), 16000)
    time.sleep(0.2)
    spk.stop()
    t = time.monotonic()
    spk.wait()
    assert sat.lines == ["STOP"] and time.monotonic() - t < 0.5
    assert sum(n for _, n in sat.audio) < 32000 * 3


def test_satellite_speaker_plays_at_the_boards_rate():
    from taco.voice.audio import SatelliteSpeaker
    sat = FakeSatForSpeaker()
    sat.speaker_rate = 48000
    spk = SatelliteSpeaker(sat)
    spk.play(np.zeros(8000, dtype=np.int16), 16000)   # 0.5 s
    spk.wait()
    assert sum(n for _, n in sat.audio) == 48000      # 24000 samples at 48 kHz


def _loop_for_replies(sat_speaker: bool, satellite_turn: bool):
    from taco.voice.main import VoiceLoop
    loop = VoiceLoop.__new__(VoiceLoop)
    loop.cfg = {"audio": {"satellite_replies": True}, "listen": {"satellite_lag_s": 0.0}}
    sat = NetworkMicrophone("living room", 0, "s3cret", host="127.0.0.1")
    sat.speaker = sat_speaker
    loop.speaker = object()
    loop._sat_speakers = {sat: object()}
    kitchen = object()
    loop.active = (sat.second if satellite_turn else kitchen, None)
    return loop, sat


def test_replies_go_to_the_satellite_for_its_turns():
    loop, sat = _loop_for_replies(sat_speaker=True, satellite_turn=True)
    assert loop._reply_speaker() is loop._sat_speakers[sat]   # even from its second channel


def test_replies_stay_in_the_kitchen_otherwise():
    loop, sat = _loop_for_replies(sat_speaker=True, satellite_turn=False)
    assert loop._reply_speaker() is loop.speaker
    loop, sat = _loop_for_replies(sat_speaker=False, satellite_turn=True)   # old firmware
    assert loop._reply_speaker() is loop.speaker


class RecordingSpeaker:
    def __init__(self):
        self.played = []

    def play(self, audio, rate):
        self.played.append(rate)

    def wait(self):
        pass


def test_wake_chime_plays_in_both_rooms_for_a_satellite_turn():
    loop, sat = _loop_for_replies(sat_speaker=True, satellite_turn=True)
    loop.speaker, loop.reply = RecordingSpeaker(), RecordingSpeaker()
    loop.flush_mics = lambda: None
    loop._play_and_wait(np.zeros(100, dtype=np.int16), 22050)
    assert loop.speaker.played == [22050] and loop.reply.played == [22050]
