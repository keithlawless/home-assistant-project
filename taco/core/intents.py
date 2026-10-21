"""Fast-path intent parsing: common requests handled without an LLM round trip.

parse() turns a transcript into an Intent or None. None means "send it to
Hermes". Matching is deliberately conservative: when unsure, defer to the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# --- numbers -----------------------------------------------------------------

_ONES = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}


def words_to_digits(text: str) -> str:
    """'twenty five minutes' -> '25 minutes'. Leaves other words alone."""
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _TENS:
            value = _TENS[tok]
            if i + 1 < len(tokens) and tokens[i + 1] in _ONES and 0 < _ONES[tokens[i + 1]] < 10:
                value += _ONES[tokens[i + 1]]
                i += 1
            out.append(str(value))
        elif tok in _ONES and tok != "oh":
            out.append(str(_ONES[tok]))
        else:
            out.append(tok)
        i += 1
    return " ".join(out)


def normalize(text: str, keep_commas: bool = False) -> str:
    text = text.lower().replace("-", " ")
    keep = r"[^\w\s:.%,]" if keep_commas else r"[^\w\s:.%]"
    text = re.sub(keep, " ", text)                 # keep 7:30, 1.5, 50% (and list commas)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)  # drop sentence periods
    text = re.sub(r"\b(\d+)(am|pm)\b", r"\1 \2", text)
    text = re.sub(r"\b([ap])\s?\.?\s?m\b", r"\1m", text)
    text = re.sub(r"\s+", " ", text).strip()
    return words_to_digits(text)


# --- durations ---------------------------------------------------------------

_UNIT_SECONDS = {"hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
_UNIT = r"(hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_DURATION_PART = re.compile(
    rf"\b(?P<num>\d+(?:\.\d+)?|an?|half an?|half a)\s*(?P<half>and a half\s+)?(?:more\s+|extra\s+)?{_UNIT}\b"
    rf"(?P<half_after>\s+and a half)?"
)
_DURATION_SPAN = re.compile(
    rf"(?:\b(?:\d+(?:\.\d+)?|an?|half an?)\s*(?:and a half\s+)?(?:more\s+|extra\s+)?{_UNIT}(?:\s+and a half)?"
    rf"(?:\s*(?:and|,)?\s*)?)+"
)


def _unit_seconds(unit: str) -> int:
    for key, secs in _UNIT_SECONDS.items():
        if unit.startswith(key):
            return secs
    raise ValueError(unit)


def parse_duration(text: str) -> float | None:
    """'1 hour and 30 minutes' -> 5400; 'an hour and a half' -> 5400. None if absent."""
    total = 0.0
    found = False
    for m in _DURATION_PART.finditer(text):
        num = m.group("num")
        if num.startswith("half"):
            value = 0.5
        elif num in ("a", "an"):
            value = 1.0
        else:
            value = float(num)
        if m.group("half") or m.group("half_after"):
            value += 0.5
        total += value * _unit_seconds(m.group(3))
        found = True
    return total if found and total > 0 else None


def strip_duration(text: str) -> str:
    return re.sub(r"\s+", " ", _DURATION_SPAN.sub(" ", text)).strip()


def say_duration(seconds: float) -> str:
    """5400 -> '1 hour and 30 minutes'; 75 -> '1 minute and 15 seconds'."""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:  # nobody needs seconds on an hour-long timer
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    if not parts:
        return "less than a second"
    return " and ".join(parts) if len(parts) <= 2 else f"{parts[0]}, {parts[1]} and {parts[2]}"


# --- clock times -------------------------------------------------------------

_CLOCK = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2})|\s(?P<m2>\d{2}))?\s*(?P<ampm>am|pm|in the morning|in the evening|at night|tonight)?\b"
)


def parse_clock(text: str, now: datetime) -> datetime | None:
    """Next occurrence of a spoken clock time: '7:30 am', '7 30', 'noon', '19:00'."""
    if re.search(r"\bnoon\b", text):
        hour, minute, meridiem = 12, 0, "pm"
    elif re.search(r"\bmidnight\b", text):
        hour, minute, meridiem = 0, 0, "am"
    else:
        m = _CLOCK.search(re.sub(r"\b(o clock|oclock)\b", "", text))
        if not m:
            return None
        hour = int(m.group("h"))
        minute = int(m.group("m") or m.group("m2") or 0)
        meridiem = m.group("ampm")
        if hour > 23 or minute > 59:
            return None
        if meridiem in ("in the morning",):
            meridiem = "am"
        elif meridiem in ("in the evening", "at night", "tonight"):
            meridiem = "pm"

    if meridiem == "am":
        candidates = [hour % 12]
    elif meridiem == "pm":
        candidates = [hour % 12 + 12]
    elif hour > 12 or hour == 0:
        candidates = [hour]
    else:
        candidates = [hour % 12, hour % 12 + 12]  # ambiguous: whichever comes next

    if re.search(r"\btomorrow\b", text):
        # Earliest matching time on tomorrow's date ("7:30 tomorrow" is the morning).
        day = now + timedelta(days=1)
        return min(day.replace(hour=h, minute=minute, second=0, microsecond=0) for h in candidates)
    best = None
    for h in candidates:
        t = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        if best is None or t < best:
            best = t
    return best


def say_clock(t: datetime, now: datetime) -> str:
    s = t.strftime("%-I:%M %p").replace(":00 ", " ")
    if t.date() == now.date():
        return s
    if t.date() == (now + timedelta(days=1)).date():
        return f"{s} tomorrow"
    return f"{s} on {t.strftime('%A')}"


# --- intents -----------------------------------------------------------------

@dataclass
class Intent:
    name: str
    slots: dict[str, Any] = field(default_factory=dict)


_FILLER = {"a", "an", "the", "my", "for", "set", "start", "create", "make", "put", "on", "up",
           "please", "can", "you", "could", "would", "taco", "hey", "new", "another", "called",
           "named", "me", "us", "timer", "timers", "and", "to", "of", "minute", "minutes", "hour",
           "hours", "second", "seconds", "just", "go", "ahead", "with", "in", "is", "it", "s",
           "cancel", "delete", "remove", "clear", "stop", "end", "kill", "turn", "off", "pause",
           "hold", "resume", "unpause", "restart", "continue", "how", "much", "long", "time",
           "left", "remaining", "what", "which", "get", "rid", "all"}

STOP_PHRASES = {
    "stop", "stop it", "stop that", "ok", "okay", "ok stop", "okay stop", "dismiss", "silence",
    "quiet", "be quiet", "shut up", "enough", "thanks", "thank you", "never mind", "nevermind",
    "cancel", "cancel that", "thats enough", "that s enough", "shush", "turn it off", "nothing",
}


def _timer_label(text: str) -> str | None:
    """Words around 'timer' that aren't filler or the duration: 'pasta', 'egg'."""
    body = strip_duration(text)
    m = re.search(r"(?:^|\s)(?P<before>(?:\w+\s){0,3})timer(?:\s(?P<after>.*))?$", body)
    if not m:
        return None
    words = [w for w in m.group("before").split() if w not in _FILLER and not w.isdigit()]
    after = (m.group("after") or "").split()
    if not words and after[:1] in (["for"], ["called"], ["named"]):
        words = [w for w in after[1:4] if w not in _FILLER and not w.isdigit()]
    label = " ".join(words).strip()
    return label or None


_FUTURE = re.compile(r"\b(tomorrow|tonight|weekend|week|monday|tuesday|wednesday|thursday|"
                     r"friday|saturday|sunday|later|forecast|next|rain|snow)\b")


def parse(raw: str, now: datetime | None = None) -> Intent | None:
    now = now or datetime.now()
    text = normalize(raw)
    if not text:
        return None

    if text in STOP_PHRASES:
        return Intent("stop")
    # Whisper often hears a spoken "play" as "place" or "plate".
    text = re.sub(r"^(?:place|plate|played|plays) (?=\w)", "play ", text)

    has_timer = re.search(r"\btimers?\b", text) is not None
    has_alarm = re.search(r"\balarms?\b", text) is not None
    duration = parse_duration(text)

    # Cancel / pause / resume a timer or alarm.
    if has_timer or has_alarm:
        kind = "alarm" if has_alarm and not has_timer else "timer"
        label = _timer_label(text.replace("alarm", "timer")) if has_alarm and not has_timer else _timer_label(text)
        if re.search(r"\b(cancel|delete|remove|clear|stop|turn off|end|kill|get rid of)\b", text):
            return Intent("cancel_timer", {"kind": kind, "label": label,
                                           "all": bool(re.search(r"\ball\b|\btimers\b|\balarms\b", text))})
        if has_timer and re.search(r"\bpause\b|\bhold\b", text):
            return Intent("pause_timer", {"label": label})
        if has_timer and re.search(r"\b(resume|unpause|restart|continue)\b", text):
            return Intent("resume_timer", {"label": label})

    # "add 2 minutes to the pasta timer", "add 5 more minutes"
    if duration and re.search(r"\b(add|put|give it|another|more|extend)\b", text) and (
            has_timer or re.search(r"\bmore\b|\bto it\b", text)):
        label = None
        if m := re.search(r"\b(?:to|on)\s+(?:the\s+)?(?P<label>[\w\s]+?)\s+timer\b", strip_duration(text)):
            label = m.group("label").strip() or None
        return Intent("add_time", {"seconds": duration, "label": label})

    # "how much time is left", "how long on the eggs"
    if re.search(r"\bhow (much time|long)\b|\btime (is )?(left|remaining)\b|\bleft on\b", text) and (
            has_timer or re.search(r"\b(left|remaining)\b|\bhow long (on|for|until|till)\b", text)):
        label = _timer_label(text) if has_timer else None
        if not label and (m := re.search(r"\b(?:left on|remaining on|long on|long for|for)\s+(?:the\s+)?(?P<l>\w+)", text)):
            label = m.group("l") if m.group("l") not in ("timer", "it", "my") else None
        return Intent("time_left", {"label": label})

    if re.search(r"\b(what|which|list|show)\b.*\btimers\b|\bany timers\b|\btimers running\b", text):
        return Intent("list_timers")

    # Set a timer: needs both the word timer and a duration.
    if has_timer and duration:
        return Intent("set_timer", {"seconds": duration, "label": _timer_label(text)})

    # Alarms: "set an alarm for 7:30", "wake me up at 6", "alarm in 20 minutes"
    if has_alarm or re.search(r"\bwake me( up)?\b", text):
        if re.search(r"\bin\b", text) and duration:
            return Intent("set_alarm", {"at": now + timedelta(seconds=duration), "label": None})
        if at := parse_clock(text, now):
            return Intent("set_alarm", {"at": at, "label": None})
        return None

    # Reminders: "remind me to X in 20 minutes" / "remind me at 5 pm to X"
    if m := re.match(r"(?:please )?remind (?:me|us) (?:to |that |about )?(?P<rest>.+)$", text):
        rest = m.group("rest")
        if duration and re.search(r"\bin\b", rest):
            msg = re.sub(r"\bin\s*$", "", strip_duration(rest)).strip()
            msg = re.sub(r"^(in\s+)?(to\s+)?", "", msg).strip()
            if msg:
                return Intent("set_reminder", {"at": now + timedelta(seconds=duration), "message": msg})
        if (cm := re.search(r"\b(?:at|by)\s+(?P<t>(?:\d{1,2}(?::\d{2}|\s\d{2})?\s*(?:am|pm)?|noon|midnight))\b",
                            rest)) and (at := parse_clock(cm.group("t"), now)):
            msg = (rest[:cm.start()] + rest[cm.end():]).strip()
            msg = re.sub(r"^(to|that|about)\s+", "", re.sub(r"\btomorrow\b", "", msg)).strip()
            if re.search(r"\btomorrow\b", rest) and at.date() == now.date():
                at += timedelta(days=1)
            if msg:
                return Intent("set_reminder", {"at": at, "message": msg})
        return None

    if re.fullmatch(r"(?:hey )?(?:what s|what is|whats|tell me) the time(?: now)?|what time is it(?: now)?", text):
        return Intent("time")
    if re.fullmatch(r"(?:what s|what is|whats) (?:the date|today s date|todays date)(?: today)?"
                    r"|what day is (?:it|today)(?: today)?", text):
        return Intent("date")

    # Current weather here. Anything about the future or another place goes to Hermes.
    if re.search(r"\b(weather|temperature|outside|hot|cold|warm)\b", text) and not _FUTURE.search(text) \
            and not re.search(r"\b(in|at|for)\s+(?!the\b|it\b|here\b|outside\b)[a-z]+", text) \
            and re.search(r"\b(what|how|whats|is it)\b", text):
        return Intent("weather_now")

    # Volume
    if re.search(r"\b(volume|louder|quieter|softer|turn (it|yourself) (up|down))\b", text):
        if m := re.search(r"\b(?:to|at)\s+(\d{1,3})\b(\s*%| percent)?|\bvolume (\d{1,2})\b", text):
            n = int(m.group(1) or m.group(3))
            level = n / 100 if (m.group(2) or n > 10) else n / 10
            return Intent("volume", {"set": max(0.0, min(1.0, level))})
        if re.search(r"\b(up|louder|raise|increase)\b", text):
            return Intent("volume", {"step": 0.1})
        if re.search(r"\b(down|quieter|softer|lower|decrease)\b", text):
            return Intent("volume", {"step": -0.1})

    # Lists keep commas so "milk, eggs and bread" splits into three items.
    return (_list_intent(normalize(raw, keep_commas=True).rstrip(",")) or _household_intent(text)
            or _speaker_intent(text) or _media_intent(text) or _recipe_intent(text)
            or _device_intent(text))


_MOVE = r"(?:switch|move|send|connect)(?: (?:it|the music|music|the audio|audio|the sound|sound))?"
_BACK = r"(?:back )?(?:here|back|(?:to|in|on) (?:the )?kitchen(?: speakers?)?)"


def _speaker_intent(text: str) -> Intent | None:
    """Which speaker music plays on (taco/core/speakers.py)."""
    if re.search(r"\bspeakers?\b", text) and (
            re.search(r"\bpair\b", text) or re.search(r"\b(?:connect|add)\b.*\bnew\b", text)):
        return Intent("pair_speaker")
    if re.search(r"\bspeakers\b", text) and re.search(r"\b(list|show|what|which|available|any)\b", text) \
            and not text.startswith("play "):
        return Intent("list_speakers")
    if re.fullmatch(rf"(?:play|{_MOVE}) {_BACK}", text) or text == "play here":
        return Intent("set_speaker", {"name": None})
    if m := (re.fullmatch(rf"(?:play|{_MOVE}) (?:on|through|over|to|over to) (?:the |my )?(?P<name>.+?)(?: speaker)?", text)
             or re.fullmatch(r"use (?:the |my )?(?P<name>.+?) speaker", text)):
        name = m.group("name")
        if name not in ("radio", "the radio", "repeat", "shuffle", "spotify"):
            return Intent("set_speaker", {"name": name})
    return None


_WHEN = {"latest": "latest", "newest": "latest", "most recent": "latest", "last": "latest", "new": "latest",
         "first": "first", "oldest": "first", "earliest": "first", "debut": "first"}
_W = "|".join(sorted(_WHEN, key=len, reverse=True))
_ALBUM_PICKS = [   # "spock s beard s latest album", "the first album by x", "the x album from 1999"
    rf"(?P<a>.+?) s (?P<w>{_W}) album",
    rf"(?P<w>{_W}) (?:album|record) (?:by|from) (?P<a>.+)",
    rf"(?P<w>{_W}) (?P<a>.+?) (?:album|record)",
    r"(?P<a>.+?) s album from (?:the year )?(?P<y>\d{4})",
    r"album (?:by|from) (?P<a>.+?) from (?:the year )?(?P<y>\d{4})",
    r"(?P<a>.+?) album from (?:the year )?(?P<y>\d{4})",
]


def _album_pick(q: str) -> dict[str, Any] | None:
    """Slots for an album chosen by time rather than by name."""
    for pattern in _ALBUM_PICKS:
        if m := re.fullmatch(pattern, q):
            g = m.groupdict()
            slots: dict[str, Any] = {"query": "", "by": g["a"], "kind": "album"}
            if g.get("w"):
                slots["pick"] = _WHEN[g["w"]]
            else:
                slots["year"] = int(g["y"])
            return slots
    return None


def _media_intent(text: str) -> Intent | None:
    """Internet radio and the music library."""
    if re.fullmatch(r"(?:stop|turn off|pause|kill|end) (?:the |this )?(?:music|radio|station|song)", text):
        return Intent("media_stop")
    if re.fullmatch(r"what s (?:playing|this|this song|this station|on)|what (?:station|song) is (?:this|playing)"
                    r"|who (?:is|s) (?:this|singing|playing)|what song is this", text):
        return Intent("media_status")
    if re.fullmatch(r"(?:re ?index|rescan|re scan|refresh|update) (?:the |my |your )?music(?: library| collection| index)?",
                    text):
        return Intent("music_reindex")
    if re.fullmatch(r"(?:next|skip)(?: this| the)?(?: song| track)|skip (?:it|this)|play the next (?:song|track)", text):
        return Intent("media_skip", {"step": 1})
    if re.fullmatch(r"(?:previous|last)(?: song| track)|go back a (?:song|track)|play the previous (?:song|track)", text):
        return Intent("media_skip", {"step": -1})
    # Only "play"/"put on": "turn on the kitchen light" is a device, not a station.
    if m := re.fullmatch(r"(?:play|put on) (?:some |the |a )?(?P<q>.+?)"
                         r"(?P<radio> radio| station| on the radio)?(?: music)?", text):
        q = m.group("q").strip()
        slots: dict[str, Any] = {}
        if m.group("radio"):
            slots["radio"] = True   # never the library
        # "play jazz on the sonos": the assistant checks it is a speaker.
        if sm := re.fullmatch(r"(?P<q>.+?) (?:on|through|over) (?:the |my )?(?P<s>[a-z0-9 ]+?)(?: speaker)?", q):
            q, slots["speaker"] = sm.group("q"), sm.group("s")
        # The library: "x s latest album", "the album X", "songs by X", "X by Y".
        if picked := _album_pick(q):
            return Intent("media_play", {**picked, **slots})
        if am := re.fullmatch(r"(?:the )?album (?P<q>.+)", q):
            q, slots["kind"] = am.group("q"), "album"
        elif am := re.fullmatch(r"(?:songs|music|something|anything|stuff) (?:by|from) (?P<q>.+)", q):
            q, slots["kind"] = am.group("q"), "artist"
        elif bm := re.fullmatch(r"(?P<q>.+?) by (?P<by>.+)", q):
            q, slots["by"] = bm.group("q"), bm.group("by")
        if q in ("music", "radio", "some music", "something", "the radio"):
            q = ""
        return Intent("media_play", {"query": q, **slots})
    return None


def _recipe_intent(text: str) -> Intent | None:
    """Hands-free recipe navigation (only acts when a recipe is open)."""
    text = text.removesuffix(" please")
    if re.fullmatch(r"(?:next|next step|what s next|what s the next step|ok next|okay next|done next|go on)", text):
        return Intent("recipe_nav", {"action": "next"})
    if re.fullmatch(r"(?:previous|previous step|go back|back|last step|the step before)", text):
        return Intent("recipe_nav", {"action": "previous"})
    if re.fullmatch(r"(?:repeat|repeat that|repeat the step|say that again|what was that|what step am i on"
                    r"|read the step|read that again)", text):
        return Intent("recipe_nav", {"action": "repeat"})
    if re.fullmatch(r"(?:read |what are )?(?:me )?the ingredients|what do i need", text):
        return Intent("recipe_nav", {"action": "ingredients"})
    if re.fullmatch(r"(?:start over|go to the beginning|first step)", text):
        return Intent("recipe_nav", {"action": "first"})
    if m := re.fullmatch(r"(?:go to |skip to )?step (?P<n>\d{1,2})", text):
        return Intent("recipe_nav", {"action": "goto", "n": int(m.group("n"))})
    if re.fullmatch(r"(?:close|exit|end|done with|finish|stop) (?:the )?(?:recipe|cooking)|i m done cooking", text):
        return Intent("recipe_nav", {"action": "close"})
    return None


_LIST = r"(?:the |my |our )?(?P<list>[a-z ]*?)\s*list"


def _list_intent(text: str) -> Intent | None:
    """Shopping / to-do lists."""
    text = re.sub(r"^(please |can you |could you )+", "", text).removesuffix(" please")
    if m := re.fullmatch(rf"(?:add|put) (?P<items>.+?) (?:to|on) {_LIST}", text):
        return Intent("list_add", {"list": m.group("list") or None, "items": m.group("items")})
    if m := re.fullmatch(rf"(?:remove|take|delete|cross|check) (?P<items>.+?) (?:from|off|off of) {_LIST}", text):
        return Intent("list_remove", {"list": m.group("list") or None, "items": m.group("items")})
    if m := re.fullmatch(rf"(?:clear|empty|erase|delete) {_LIST}", text):
        return Intent("list_clear", {"list": m.group("list") or None})
    if m := re.fullmatch(rf"(?:what s|what is|whats|read|read me|tell me what s|what do i have) (?:on )?{_LIST}"
                         rf"(?: say)?", text):
        return Intent("list_read", {"list": m.group("list") or None})
    if re.fullmatch(r"what do (?:i|we) need (?:from|at) the (?:store|grocery store|supermarket)", text):
        return Intent("list_read", {"list": "shopping"})
    return None


def _household_intent(text: str) -> Intent | None:
    """Scenes, the morning briefing, and muting the microphone."""
    if re.fullmatch(r"(?:stop listening|mute(?: the)? (?:microphone|mic)|turn off(?: the)? (?:microphone|mic)"
                    r"|privacy mode|don t listen)", text):
        return Intent("mic_mute")
    if re.fullmatch(r"(?:what s|whats|give me) (?:my |the )?(?:briefing|morning briefing|daily briefing|day look like"
                    r"|rundown)|(?:what s|whats) (?:going on )?today|brief me", text):
        return Intent("briefing")
    if m := re.fullmatch(r"(?:save|remember|store) (?:this|these|the lights|this setting|these settings|the current"
                         r"(?: lights| setting| settings)?) as (?P<name>[a-z0-9 ]{2,40}?)(?: scene| mode)?", text):
        return Intent("scene_save", {"name": m.group("name")})
    if m := re.fullmatch(r"(?:run|activate|start|set|turn on|switch to|begin) (?:the )?(?P<name>[a-z0-9 ]{2,40}?)"
                         r"(?: scene| mode| routine)", text):
        return Intent("scene", {"name": m.group("name")})
    return None


# Pronouns need conversation context, which only Hermes has.
_PRONOUNS = {"it", "that", "this", "them", "those", "these", "everything else"}


def _device_intent(text: str) -> Intent | None:
    """Turn devices on/off, set a level, or ask whether one is on."""
    text = re.sub(r"^(please |can you |could you |would you )+", "", text).removesuffix(" please")
    if text in ("lights out", "lights off", "all off"):
        return Intent("power", {"on": False, "target": "everything"})
    if text in ("lights on", "all on"):
        return Intent("power", {"on": True, "target": "everything"})

    # Ask before act: "is the lamp on" must not match "<target> on".
    if m := re.fullmatch(r"(?:is|are) (?:the )?(?P<t>.+?) (?:on|off|still on)", text):
        if m.group("t") not in _PRONOUNS:
            return Intent("device_status", {"target": m.group("t")})
    m = (re.fullmatch(r"(?:turn|switch|shut|power) (?P<onoff>on|off) (?P<t>.+)", text)
         or re.fullmatch(r"(?:turn|switch|shut|power) (?P<t>.+?) (?P<onoff>on|off)", text)
         or re.fullmatch(r"(?P<t>.+?) (?P<onoff>on|off)", text))
    if m and m.group("t") not in _PRONOUNS and len(m.group("t").split()) <= 6:
        return Intent("power", {"on": m.group("onoff") == "on", "target": m.group("t")})

    if m := re.fullmatch(r"(?:set|dim|turn|bring|put) (?:the )?(?P<t>.+?) (?:to|at|down to|up to) "
                         r"(?P<n>\d{1,3})(?:\s*%| percent)?", text):
        if m.group("t") not in _PRONOUNS:
            return Intent("level", {"target": m.group("t"), "level": min(100, int(m.group("n")))})
    if m := re.fullmatch(r"(?P<dir>dim|brighten) (?:the )?(?P<t>.+)", text):
        if m.group("t") not in _PRONOUNS:
            return Intent("level", {"target": m.group("t"), "level": 30 if m.group("dir") == "dim" else 100})

    return None
