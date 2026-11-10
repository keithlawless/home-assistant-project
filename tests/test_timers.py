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


def test_room_is_kept_and_old_tables_migrate(tmp_path):
    import sqlite3
    db_path = tmp_path / "t.db"
    # A table from before rooms existed.
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE timers (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        label TEXT, duration_s REAL, ends_at REAL NOT NULL, state TEXT NOT NULL, remaining_s REAL,
        message TEXT, created_at REAL NOT NULL, rang_at REAL)""")
    conn.execute("INSERT INTO timers (kind, ends_at, state, created_at) VALUES ('timer', 1, 'running', 0)")
    conn.commit()
    conn.close()
    store = Database(db_path).timers
    assert [t.room for t in store.all()] == [None]
    store.add_alarm(time.time() + 3600, room="bedroom")
    store.add_reminder(time.time() + 60, "bread", room="")
    rooms = {t.kind: t.room for t in Database(db_path).timers.all()}
    assert rooms == {"timer": None, "alarm": "bedroom", "reminder": None}
    assert store.find(None, "alarm")[0].to_dict()["room"] == "bedroom"
