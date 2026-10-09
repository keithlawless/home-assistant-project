import time

from taco.core.db import Database


def test_timer_lifecycle(tmp_path, monkeypatch):
    store = Database(tmp_path / "t.db").timers
    t = store.add_timer(60, "pasta")
    assert t.state == "running" and 59 < t.remaining() <= 60

    t = store.pause(t)
    assert t.state == "paused"
    t = store.add_time(t, 30)
    assert 89 < t.remaining() <= 90
    t = store.resume(t)
    assert t.state == "running"

    # Jump past the end: the tick moves it to ringing, stop removes it.
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 200)
    assert store.tick()
    assert [x.label for x in store.ringing()] == ["pasta"]
    assert [x.label for x in store.stop_ringing()] == ["pasta"]
    assert store.all() == []


def test_find_by_label_and_persistence(tmp_path):
    db_path = tmp_path / "t.db"
    store = Database(db_path).timers
    store.add_timer(60, "rice")
    store.add_timer(120, "chicken")
    store.add_alarm(time.time() + 3600)
    assert [t.label for t in store.find("the rice", "timer")] == ["rice"]
    assert len(store.find(None, "timer")) == 2
    # Survives a restart.
    assert len(Database(db_path).timers.all()) == 3
