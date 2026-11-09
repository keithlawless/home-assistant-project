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
    def __init__(self):
        self.lines = []
        self.second = object()   # its front end's second channel

    def send_line(self, line):
        self.lines.append(line)


def _loop_with_satellite():
    from taco.voice.main import VoiceLoop

    loop = VoiceLoop.__new__(VoiceLoop)   # no models or devices
    sat = FakeSat()
    loop.listeners = [(object(), None), (sat, None)]
    loop.active = loop.listeners[0]
    loop.state, loop.muted = "idle", False
    loop.events = type("E", (), {"send": lambda self, *a, **k: None})()
    loop._satellites = lambda: [sat]
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
