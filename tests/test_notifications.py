import sqlite3
import threading
from datetime import datetime

from taco.core import notifications as N
from taco.core.notifications import NotificationStore, in_window


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def store(path=":memory:", clock=None):
    return NotificationStore(sqlite3.connect(path, check_same_thread=False), threading.Lock(), clock or Clock())


def test_cards_stack_newest_first():
    clock = Clock()
    s = store(clock=clock)
    s.add("info", "first")
    clock.t += 1
    s.add("camera", "second")
    assert [c["title"] for c in s.active()] == ["second", "first"]


def test_same_key_merges_within_window_then_stacks():
    clock = Clock()
    s = store(clock=clock)
    s.add("camera", "Motion", key="alexa:front door")
    clock.t += 60
    card = s.add("camera", "Motion again", key="alexa:front door")
    assert card["count"] == 2 and card["title"] == "Motion again" and len(s.active()) == 1
    clock.t += N.MERGE_S + 1
    s.add("camera", "Motion later", key="alexa:front door")
    assert len(s.active()) == 2


def test_merge_forever_and_resolve():
    clock = Clock()
    s = store(clock=clock)
    s.add("health", "CPU busy", key="health:cpu", merge_s=None)
    clock.t += 3600
    assert s.resolve("health:cpu")
    assert s.active()[0]["resolved"] == clock.t
    card = s.add("health", "CPU busy again", key="health:cpu", merge_s=None)
    assert card["resolved"] is None and card["count"] == 2 and len(s.active()) == 1


def test_unknown_kind_becomes_info():
    assert store().add("<script>", "x")["kind"] == "info"


def test_dismiss_and_dismiss_all():
    s = store()
    a = s.add("info", "a")
    s.add("info", "b")
    assert s.dismiss(a["id"]) and not s.dismiss(a["id"])
    assert s.dismiss_all() == 1 and s.active() == []


def test_webhook_cards_capped_local_ones_kept():
    clock = Clock()
    s = store(clock=clock)
    s.add("health", "local")
    for i in range(N.MAX_FROM_WEBHOOK + 5):
        clock.t += 1
        s.add("camera", f"outside {i}", sender="alexa")
    cards = s.active()
    outside = [c for c in cards if c["sender"]]
    assert len(outside) == N.MAX_FROM_WEBHOOK
    assert outside[0]["title"] == f"outside {N.MAX_FROM_WEBHOOK + 4}"   # newest kept
    assert any(c["title"] == "local" for c in cards)


def test_persists(tmp_path):
    path = str(tmp_path / "n.db")
    store(path).add("info", "kept")
    assert store(path).active()[0]["title"] == "kept"


def test_quiet_window_across_midnight():
    at = lambda h, m=0: datetime(2026, 10, 15, h, m)
    assert in_window(at(22), "22:00", "06:00")
    assert in_window(at(2, 30), "22:00", "06:00")
    assert not in_window(at(6), "22:00", "06:00")
    assert not in_window(at(21, 59), "22:00", "06:00")
    assert in_window(at(13), "12:00", "14:00") and not in_window(at(14), "12:00", "14:00")
