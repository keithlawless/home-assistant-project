import threading
import time

from taco.voice.alarm import AlarmPlayer


class FakeSpeaker:
    def __init__(self, name):
        self.name = name
        self.played = 0
        self.stops = 0

    def play(self, audio, rate):
        self.played += 1

    def wait(self):
        time.sleep(0.01)

    def stop(self):
        self.stops += 1


def _ringing(i, room=None, state="ringing"):
    return {"id": i, "kind": "alarm", "label": None, "message": None, "state": state, "room": room}


def _until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_rings_in_the_room_it_was_set_from():
    kitchen, bedroom = FakeSpeaker("kitchen"), FakeSpeaker("bedroom")
    rooms = {"bedroom": bedroom}
    player = AlarmPlayer(lambda room: rooms.get(room, kitchen), lambda text: (None, 22050))
    player.update([_ringing(1, "bedroom")])
    assert _until(lambda: bedroom.played >= 2)   # announcement, then chimes
    assert kitchen.played == 0
    player.update([])
    assert bedroom.stops == 1 and kitchen.stops == 0


def test_timer_updates_do_not_stop_a_reply():
    """A satellite's speaker also plays replies; "timer set" mustn't cut one off."""
    speaker = FakeSpeaker("living room")
    player = AlarmPlayer(lambda room: speaker, lambda text: (None, 22050))
    player.update([_ringing(1, "living room", state="running")])
    player.update([])
    assert speaker.stops == 0 and not player.active


def test_falls_back_when_the_room_speaker_goes_away():
    kitchen, bedroom = FakeSpeaker("kitchen"), FakeSpeaker("bedroom")
    online = threading.Event()
    online.set()
    player = AlarmPlayer(lambda room: bedroom if online.is_set() else kitchen, lambda text: (None, 22050))
    player.update([_ringing(1, "bedroom")])
    assert _until(lambda: bedroom.played >= 1)
    online.clear()
    assert _until(lambda: kitchen.played >= 1)
    player.silence()
    assert bedroom.stops == 1 and kitchen.stops == 1
