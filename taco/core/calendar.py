"""Calendar events from private iCal (.ics) addresses, read-only.

Google Calendar: Settings -> (your calendar) -> Integrate calendar ->
"Secret address in iCal format". Put the URL(s) in /srv/taco/config.toml:

    [calendar]
    ical_urls = ["https://calendar.google.com/calendar/ical/.../basic.ics"]

The address is a secret (anyone with it can read the calendar), so it lives
only in the Pi's config file, never in the repo.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
import icalendar
import recurring_ical_events

log = logging.getLogger(__name__)


async def fetch_events(http: httpx.AsyncClient, urls: list[str], start: date, days: int = 7) -> list[dict[str, Any]]:
    """Events from `start` for `days` days, expanded for recurrence, sorted by time."""
    events: list[dict[str, Any]] = []
    begin = datetime.combine(start, time.min)
    end = begin + timedelta(days=days)
    for url in urls:
        try:
            r = await http.get(url, follow_redirects=True)
            r.raise_for_status()
            cal = icalendar.Calendar.from_ical(r.content)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("calendar fetch failed: %s", e)
            continue
        for ev in recurring_ical_events.of(cal).between(begin, end):
            dtstart = ev.get("DTSTART").dt
            all_day = not isinstance(dtstart, datetime)
            if isinstance(dtstart, datetime) and dtstart.tzinfo:
                dtstart = dtstart.astimezone().replace(tzinfo=None)   # house local time
            events.append({
                "title": str(ev.get("SUMMARY", "Untitled")),
                "date": (dtstart if all_day else dtstart.date()).isoformat(),
                "time": None if all_day else dtstart.strftime("%H:%M"),
                "all_day": all_day,
                "location": str(ev.get("LOCATION", "")) or None,
            })
    return sorted(events, key=lambda e: (e["date"], e["time"] or ""))


def say_events(events: list[dict[str, Any]]) -> str:
    """'Dentist at 3 PM and book club at 7:30 PM' (all-day events first)."""
    parts = []
    for e in events:
        if e["all_day"]:
            parts.append(f"{e['title']} all day")
        else:
            t = datetime.strptime(e["time"], "%H:%M").strftime("%-I:%M %p").replace(":00 ", " ")
            parts.append(f"{e['title']} at {t}")
    return ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else (parts[0] if parts else "")
