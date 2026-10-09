import asyncio
import sqlite3
import threading
from datetime import datetime

from taco.core.scheduler import Scheduler, ScheduleStore, describe, trigger_time

SUN = {"sunrise": "2026-10-09T06:52", "sunset": "2026-10-09T18:14"}


def make():
    store = ScheduleStore(sqlite3.connect(":memory:", check_same_thread=False), threading.Lock())
    ran = []

    async def run(actions):
        ran.append(actions)

    async def power(ids, on):
        pass

    sched = Scheduler(store, run, lambda: SUN, lambda: False, lambda: [], power)
    return store, sched, ran


def test_trigger_times():
    day = datetime(2026, 10, 9, 12, 0)
    assert trigger_time({"at": "06:30"}, day, SUN) == datetime(2026, 10, 9, 6, 30)
    assert trigger_time({"sun": "sunset", "offset_min": -15}, day, SUN) == datetime(2026, 10, 9, 17, 59)
    assert trigger_time({"sun": "sunset"}, day, None) is None


def test_fires_once_per_day_on_chosen_days():
    store, sched, ran = make()
    store.add("porch", {"sun": "sunset"}, [], [{"type": "say", "text": "hi"}])
    store.add("weekday alarm", {"at": "06:30"}, [0, 1, 2, 3, 4], [{"type": "alarm"}])
    friday_evening = datetime(2026, 10, 9, 18, 15)
    assert asyncio.run(sched.tick(friday_evening)) == ["porch"]
    assert asyncio.run(sched.tick(friday_evening)) == []                         # already ran today
    assert asyncio.run(sched.tick(datetime(2026, 10, 10, 6, 31))) == []          # Saturday: not a weekday
    assert asyncio.run(sched.tick(datetime(2026, 10, 12, 6, 31))) == ["weekday alarm"]
    assert asyncio.run(sched.tick(datetime(2026, 10, 13, 9, 0))) == []           # too late to catch up


def test_describe():
    store, _, _ = make()
    s = store.add("x", {"at": "06:30"}, [0, 1, 2, 3, 4], [])
    assert describe(s) == "every weekday at 6:30 AM"
    s = store.add("y", {"sun": "sunset", "offset_min": -15}, [], [])
    assert describe(s) == "every day 15 minutes before sunset"
