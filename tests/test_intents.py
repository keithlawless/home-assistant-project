from datetime import datetime

import pytest

from taco.core.intents import (normalize, parse, parse_clock, parse_duration, say_clock,
                               say_duration)

NOW = datetime(2026, 10, 9, 11, 0)  # Friday 11:00 AM


def p(text):
    return parse(text, NOW)


@pytest.mark.parametrize("text,seconds", [
    ("9 minutes", 540),
    ("1 hour and 30 minutes", 5400),
    ("an hour and a half", 5400),
    ("half an hour", 1800),
    ("90 seconds", 90),
    ("2 and a half minutes", 150),
    ("1.5 hours", 5400),
    ("a minute", 60),
    ("twenty five minutes", 1500),
    ("3 min 30 sec", 210),
])
def test_parse_duration(text, seconds):
    assert parse_duration(normalize(text)) == seconds


@pytest.mark.parametrize("text,label,seconds", [
    ("Set a timer for 9 minutes.", None, 540),
    ("Set a pasta timer for 9 minutes.", "pasta", 540),
    ("Set a 10 minute timer for the eggs", "eggs", 600),
    ("Start a five-minute egg timer", "egg", 300),
    ("Timer for 20 minutes.", None, 1200),
    ("Set a timer for an hour and a half called roast", "roast", 5400),
    ("Can you set a rice timer for eighteen minutes?", "rice", 1080),
])
def test_set_timer(text, label, seconds):
    intent = p(text)
    assert intent.name == "set_timer"
    assert intent.slots == {"seconds": seconds, "label": label}


def test_time_left():
    assert p("How much time is left?").name == "time_left"
    i = p("How much time is left on the pasta timer?")
    assert i.name == "time_left" and i.slots["label"] == "pasta"
    assert p("How long on the eggs?").slots["label"] == "eggs"


def test_cancel_and_add():
    i = p("Cancel the pasta timer.")
    assert i.name == "cancel_timer" and i.slots["label"] == "pasta" and not i.slots["all"]
    assert p("Cancel all timers").slots["all"]
    assert p("Stop the timer").name == "cancel_timer"
    i = p("Add 2 minutes to the pasta timer")
    assert i.name == "add_time" and i.slots == {"seconds": 120, "label": "pasta"}
    assert p("Add five more minutes").slots == {"seconds": 300, "label": None}
    assert p("Pause the timer").name == "pause_timer"
    assert p("What timers do I have?").name == "list_timers"


@pytest.mark.parametrize("text", ["Stop.", "stop", "OK", "Thank you.", "Never mind"])
def test_stop(text):
    assert p(text).name == "stop"


def test_alarms_and_clock():
    assert p("Set an alarm for 7:30 am").slots["at"] == datetime(2026, 10, 10, 7, 30)
    assert p("Wake me up at 6").slots["at"] == datetime(2026, 10, 9, 18, 0)  # next 6 o'clock
    assert p("Set an alarm for 2 pm").slots["at"] == datetime(2026, 10, 9, 14, 0)
    assert p("Set an alarm for noon").slots["at"] == datetime(2026, 10, 9, 12, 0)
    assert p("Set an alarm in 20 minutes").slots["at"] == datetime(2026, 10, 9, 11, 20)
    assert p("set an alarm for seven thirty tomorrow").slots["at"] == datetime(2026, 10, 10, 7, 30)
    assert parse_clock("19:15", NOW) == datetime(2026, 10, 9, 19, 15)
    assert p("Cancel my alarm").slots["kind"] == "alarm"


def test_reminders():
    i = p("Remind me to flip the chicken in 10 minutes")
    assert i.name == "set_reminder"
    assert i.slots == {"at": datetime(2026, 10, 9, 11, 10), "message": "flip the chicken"}
    i = p("Remind me at 5 pm to start dinner")
    assert i.slots == {"at": datetime(2026, 10, 9, 17, 0), "message": "start dinner"}
    i = p("Remind me to take out the trash tomorrow at 7 am")
    assert i.slots == {"at": datetime(2026, 10, 10, 7, 0), "message": "take out the trash"}


def test_time_date_weather_volume():
    assert p("What time is it?").name == "time"
    assert p("What's the date today?").name == "date"
    assert p("What's the weather?").name == "weather_now"
    assert p("How cold is it outside?").name == "weather_now"
    assert p("What's the weather in Boston?") is None          # another place -> Hermes
    assert p("What's the weather tomorrow?") is None           # future -> Hermes
    assert p("Turn the volume up").slots == {"step": 0.1}
    assert p("Quieter").slots == {"step": -0.1}
    assert p("Set the volume to 6").slots == {"set": 0.6}
    assert p("volume 40 percent").slots == {"set": 0.4}


@pytest.mark.parametrize("text", [
    "What goes well with tomatoes?",
    "Who won the World Series last year?",
    "How many cups are in a quart?",
    "Turn that on",                     # pronouns need Hermes's context
    "Tell me a joke",
])
def test_defers_to_hermes(text):
    assert p(text) is None


def test_say_helpers():
    assert say_duration(5400) == "1 hour and 30 minutes"
    assert say_duration(75) == "1 minute and 15 seconds"
    assert say_duration(3725) == "1 hour and 2 minutes"
    assert say_clock(datetime(2026, 10, 10, 7, 30), NOW) == "7:30 AM tomorrow"
    assert say_clock(datetime(2026, 10, 9, 14, 0), NOW) == "2 PM"


@pytest.mark.parametrize("text,on,target", [
    ("Turn on the kitchen light.", True, "the kitchen light"),
    ("Turn the living room off", False, "the living room"),
    ("Switch off bedroom lamp one", False, "bedroom lamp 1"),
    ("Living room lamps on", True, "living room lamps"),
    ("Please turn off all the lights", False, "all the lights"),
    ("Lights out", False, "everything"),
])
def test_power(text, on, target):
    i = p(text)
    assert i.name == "power" and i.slots == {"on": on, "target": target}


def test_level_and_status():
    assert p("Set the living room lamp to 40%").slots == {"target": "living room lamp", "level": 40}
    assert p("Dim the kitchen lights").slots == {"target": "kitchen lights", "level": 30}
    assert p("Is the bedroom lamp on?").slots == {"target": "bedroom lamp"}
    assert p("Set the volume to 6").name == "volume"   # volume wins over device level


def test_lists():
    i = p("Add milk, eggs and bread to the shopping list")
    assert i.name == "list_add" and i.slots == {"list": "shopping", "items": "milk, eggs and bread"}
    assert p("Put paper towels on my list").slots == {"list": None, "items": "paper towels"}
    assert p("Add call the plumber to my to do list").slots["list"] == "to do"
    assert p("Remove eggs from the shopping list").name == "list_remove"
    assert p("What's on the shopping list?").slots == {"list": "shopping"}
    assert p("What's on my list?").slots == {"list": None}
    assert p("Clear the grocery list").slots == {"list": "grocery"}
    assert p("What do we need from the store?").name == "list_read"


def test_household():
    assert p("Stop listening").name == "mic_mute"
    assert p("What's my briefing?").name == "briefing"
    assert p("Save this as movie time").slots == {"name": "movie time"}
    assert p("Activate movie time mode").slots == {"name": "movie time"}
    assert p("Turn on the living room") .name == "power"   # not a scene


def test_media_and_recipe():
    assert p("Play WBUR").slots == {"query": "wbur"}
    assert p("Play some jazz radio").slots == {"query": "jazz", "radio": True}
    assert p("Play some music").slots == {"query": ""}
    assert p("Stop the music").name == "media_stop"
    assert p("What's playing?").name == "media_status"
    assert p("Next step").slots == {"action": "next"}
    assert p("Repeat that").slots == {"action": "repeat"}
    assert p("Read me the ingredients").slots == {"action": "ingredients"}
    assert p("Go to step 4").slots == {"action": "goto", "n": 4}
    assert p("Close the recipe").slots == {"action": "close"}
    assert p("Turn on the kitchen light").name == "power"     # not radio


@pytest.mark.parametrize("text,name,slots", [
    ("Play on Sonos", "set_speaker", {"name": "sonos"}),
    ("Play on the Sonos speaker", "set_speaker", {"name": "sonos"}),
    ("Switch to Sonos", "set_speaker", {"name": "sonos"}),
    ("Send the music to the Sonos", "set_speaker", {"name": "sonos"}),
    ("Connect to the Sonos", "set_speaker", {"name": "sonos"}),
    ("Use the Sonos speaker", "set_speaker", {"name": "sonos"}),
    ("Play here", "set_speaker", {"name": None}),
    ("Switch back", "set_speaker", {"name": None}),
    ("Switch back to the kitchen", "set_speaker", {"name": None}),
    ("Play on the kitchen speaker", "set_speaker", {"name": None}),
    ("List speakers", "list_speakers", {}),
    ("What speakers are available?", "list_speakers", {}),
    ("Which speakers can you play on?", "list_speakers", {}),
    ("Pair a new speaker", "pair_speaker", {}),
    ("Connect a new Bluetooth speaker", "pair_speaker", {}),
    ("Play jazz on Sonos", "media_play", {"query": "jazz", "speaker": "sonos"}),
    ("Play music on the Sonos", "media_play", {"query": "", "speaker": "sonos"}),
    ("Put on some jazz", "media_play", {"query": "jazz"}),
    ("Play WBUR on the radio", "media_play", {"query": "wbur", "radio": True}),
    ("Play the album Our Time in Eden", "media_play", {"query": "our time in eden", "kind": "album"}),
    ("Play songs by Aerosmith", "media_play", {"query": "aerosmith", "kind": "artist"}),
    ("Play Noah's Dove by 10,000 Maniacs", "media_play", {"query": "noah s dove", "by": "10 000 maniacs"}),
    ("Play Aerosmith on the Sonos", "media_play", {"query": "aerosmith", "speaker": "sonos"}),
    ("Next song", "media_skip", {"step": 1}),
    ("Skip this track", "media_skip", {"step": 1}),
    ("Previous song", "media_skip", {"step": -1}),
    ("Reindex music", "music_reindex", {}),
    ("Re-index the music library", "music_reindex", {}),
    ("Next", "recipe_nav", {"action": "next"}),
])
def test_speakers(text, name, slots):
    intent = p(text)
    assert (intent.name, intent.slots) == (name, slots)
