from taco.core.devices.match import guess_room, resolve

ROOMS = ["Kitchen", "Living Room", "Dining Room", "Family Room", "Bedroom"]
DEVICES = [
    {"id": 1, "name": "Living Room Lamp One", "room": "Living Room", "aliases": []},
    {"id": 2, "name": "Living Room Lamp Two", "room": "Living Room", "aliases": ["reading lamp"]},
    {"id": 3, "name": "Bedroom Lamp One", "room": "Bedroom", "aliases": []},
    {"id": 4, "name": "Coffee Maker", "room": "Kitchen", "aliases": []},
]


def r(target):
    return resolve(target, DEVICES, ROOMS)


def test_exact_device_beats_room():
    m = r("the living room lamp 1")
    assert (m.scope, m.ids, m.ambiguous) == ("device", [1], False)


def test_room_and_room_with_generic_words():
    assert (r("the living room").scope, r("the living room").ids) == ("room", [1, 2])
    assert r("living room lamps").ids == [1, 2]
    assert r("kitchen lights").ids == [4]


def test_alias_partial_ambiguous_all():
    assert r("the reading lamp").ids == [2]
    assert r("coffee maker").ids == [4]
    m = r("lamp one")
    assert m.ambiguous and m.ids == [1, 3]
    assert r("everything").scope == "all"
    assert r("the garage") is None


def test_empty_room_and_guess():
    m = r("dining room")
    assert m.scope == "room" and m.ids == []
    assert guess_room("Living Room Lamp One", ROOMS) == "Living Room"
    assert guess_room("Bedroom Lamp Two", ROOMS) == "Bedroom"
    assert guess_room("Smart Plug", ROOMS) is None
