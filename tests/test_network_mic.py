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
