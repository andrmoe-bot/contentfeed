"""Parse iCalendar resources into ContentFeed's normalized post shape. The icalendar library reads the
format (quoting, escapes, time zones, durations) and recurring_ical_events expands repeating events and
tasks, with their excluded dates and edited instances; this module maps the results to posts."""

import re
import time as clock_time
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, TypedDict

import icalendar
import recurring_ical_events


# Read tasks as leniently as the library reads events: a value it can't parse (PRIORITY:high) is kept as
# broken and the rest of the task is read, instead of the whole calendar failing.
icalendar.Todo.ignore_exceptions = True


class ICalendarError(ValueError):
    """The downloaded resource is not a complete iCalendar object."""


class CalendarPost(TypedDict):
    """Normalized calendar item consumed by the feed and ranking layers."""

    url: str
    title: str
    description: str
    published: float | None
    calendar_kind: Literal["event", "task"]
    calendar_start: float | None
    deadline: float | None
    calendar_uid: str
    priority: int | None  # PRIORITY 1 (highest) to 9 (lowest); None if undefined


WINDOW_BEFORE = 86400  # repeating items are expanded from a day ago...
WINDOW_AFTER = 90 * 86400  # ...to 90 days ahead
ONE_SECOND = timedelta(seconds=1)


def _timestamp(value, floating=None) -> float | None:
    """A date or time as a unix time. Floating times and dates, which have no time zone, are in the
    server's local time (or in floating, if given); a date is its midnight."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=floating) if floating else value.astimezone()).timestamp()
    if isinstance(value, date):
        return _timestamp(datetime.combine(value, time.min), floating)
    return None


def _value(component, name):
    """A date or time property's value, or None, also when it couldn't be parsed."""
    try:
        return getattr(component.get(name), "dt", None)
    except ValueError:
        return None


def _text(component, name) -> str:
    return str(component.get(name, "")).strip()


def _calendar(text) -> icalendar.Calendar:
    try:
        return icalendar.Calendar.from_ical(text)
    except (ValueError, IndexError, KeyError) as e:
        raise ICalendarError(f"The calendar couldn't be read: {e}") from e


def _repeats(component) -> bool:
    return bool(component.get("RRULE") or component.get("RDATE")) and (
        component.get("DTSTART") is not None or component.get("DUE") is not None)


def _window():
    now = clock_time.time()
    return (datetime.fromtimestamp(now - WINDOW_BEFORE, timezone.utc),
            datetime.fromtimestamp(now + WINDOW_AFTER, timezone.utc))


def _series(components, extras) -> icalendar.Calendar:
    """A calendar holding just these components (one UID's), with the calendar-wide properties that
    affect how their times are read."""
    sub = icalendar.Calendar()
    for name, value in extras:
        sub.add(name, value)
    for component in components:
        sub.add_component(component)
    return sub


def _instances(master, overrides, extras) -> list:
    """A repeating series' instances in the window, with its edited instances in place of the ones they
    replace. Edited instances the series no longer has (it was shortened or the date excluded since)
    are left out. When none fall in the window, the first is kept if it's still ahead."""
    lower, upper = _window()
    valid = [o for o in overrides if _is_instance(master, _value(o, "RECURRENCE-ID"), extras)]
    for edit in valid:
        if edit.get("DTSTART") is None and edit.get("DUE") is None:
            # Such as a cancelled instance written as just its RECURRENCE-ID: it's at the time it replaces.
            edit.add("DTSTART" if master.get("DTSTART") is not None else "DUE", _value(edit, "RECURRENCE-ID"))
    query = recurring_ical_events.of(_series([master, *valid], extras), components=[master.name])
    instances = list(query.between(lower, upper))
    if not instances:
        start = _value(master, "DTSTART") or _value(master, "DUE")
        if _timestamp(start) is not None and _timestamp(start) >= lower.timestamp():
            instances = [i for i in query.between(start, start + ONE_SECOND)
                         if _value(i, "RECURRENCE-ID") == start]
    return instances


def _is_instance(master, recurrence, extras) -> bool:
    """Whether the repeating master still has an instance at recurrence."""
    if recurrence is None:
        return False
    query = recurring_ical_events.of(_series([master], extras), components=[master.name])
    target = _timestamp(recurrence)
    return any(_timestamp(_value(i, "RECURRENCE-ID")) == target
               for i in query.between(recurrence - ONE_SECOND, recurrence + ONE_SECOND))


def _post(component, source, resource_url) -> CalendarPost | None:
    """One post for an event or task instance, or None if it's cancelled or done. source is the
    component as written, which says whether a task has a start of its own (the library fills one in)."""
    kind = "event" if component.name == "VEVENT" else "task"
    status = _text(component, "STATUS").upper()
    if (kind == "task" and (status in ("COMPLETED", "CANCELLED") or component.get("COMPLETED"))) or (
            kind == "event" and status == "CANCELLED"):
        return None
    title = _text(component, "SUMMARY") or "Untitled calendar item"
    start_value = _value(component, "DTSTART") if source.get("DTSTART") is not None else None
    start = _timestamp(start_value)
    duration = _value(component, "DURATION")
    if kind == "event":
        end = _value(component, "DTEND")
        if end is None and duration is not None and start_value is not None:
            end = start_value + duration
        deadline = _timestamp(end) if end is not None and _timestamp(end) != start else None
    else:
        due = _value(component, "DUE")
        if due is None and duration is not None and start_value is not None:
            due = start_value + duration
        deadline = _timestamp(due)
    priority = _text(component, "PRIORITY")
    priority = int(priority) if re.fullmatch(r"[1-9]", priority) else None  # 0 means undefined
    uid = _text(component, "UID") or title
    # An instance is told apart by the time it was scheduled for. Floating times and dates count as UTC
    # here, as they did before times were read by the library, so the posts' urls (and with them
    # what's been viewed or skipped) stay the same.
    occurrence = _value(component, "RECURRENCE-ID") or start_value or (
        _value(component, "DUE") if kind == "task" else None)
    stamp = _timestamp(occurrence, floating=timezone.utc)
    stable = urllib.parse.quote(uid, safe="") + (f"-{int(stamp)}" if stamp else "")
    external = _text(component, "URL")
    target = external if external.startswith(("http://", "https://")) else resource_url
    parsed_target = urllib.parse.urlsplit(target)
    fragment = f"{parsed_target.fragment}&item={stable}" if parsed_target.fragment else f"item={stable}"
    url = urllib.parse.urlunsplit(parsed_target._replace(fragment=fragment))
    published = start if kind == "event" else (deadline or start or _timestamp(_value(component, "CREATED")))
    return {
        "url": url, "title": title[:300], "description": _text(component, "DESCRIPTION")[:500],
        "published": published, "calendar_kind": kind, "calendar_start": start,
        "deadline": deadline, "calendar_uid": uid, "priority": priority,
    }


def _items(calendar: icalendar.Calendar, resource_url) -> list[CalendarPost]:
    extras = [(name, calendar[name]) for name in ("X-WR-TIMEZONE",) if name in calendar]
    masters, overrides, order = {}, {}, []
    for component in calendar.subcomponents:
        if component.name not in ("VEVENT", "VTODO"):
            continue
        uid = _text(component, "UID")
        if uid not in masters and uid not in overrides:
            order.append(uid)
        if component.get("RECURRENCE-ID") is not None:
            overrides.setdefault(uid, []).append(component)
        else:
            masters.setdefault(uid, component)
    out = []
    for uid in order:
        master = masters.get(uid)
        edits = overrides.get(uid, [])
        if master is not None and _repeats(master):
            sources = {_timestamp(_value(o, "RECURRENCE-ID")): o for o in edits}
            try:
                instances = _instances(master, edits, extras)
            except Exception:  # a rule the library can't expand: keep the series' first instance
                instances = [master]
            for instance in sorted(instances, key=lambda i: _timestamp(_value(i, "RECURRENCE-ID")) or 0):
                source = sources.get(_timestamp(_value(instance, "RECURRENCE-ID")), master)
                out.append(_post(instance, source, resource_url))
        else:
            # Not repeating: kept whenever it is. Edited instances of a series stored elsewhere
            # (another CalDAV resource) are kept as they are.
            out.extend(_post(component, component, resource_url) for component in ([master] if master is not None else []) + edits)
    return [post for post in out if post is not None]


def parse_calendar_resource(text, resource_url) -> list[CalendarPost]:
    """Convert one VCALENDAR resource into the app's normalized post dictionaries. A resource that
    can't be read gives none, so one bad entry doesn't hide the rest of a CalDAV calendar."""
    try:
        return _items(_calendar(text), resource_url)
    except ICalendarError:
        return []


def parse_calendar_data(body, resource_url):
    text = body.decode("utf-8-sig", errors="replace")
    if not re.search(r"(?im)^BEGIN:VCALENDAR\s*$", text) or not re.search(r"(?im)^END:VCALENDAR\s*$", text):
        raise ICalendarError("The .ics response is not a complete iCalendar object.")
    return calendar_result(_items(_calendar(text), resource_url), None, resource_url)


MAX_ENTRIES = 1000  # tasks, and separately events, kept per calendar
# Saved with each calendar read ("parser"). Raise it whenever a change here would read calendars
# differently: at startup, calendars last read with another version are read again (see server.py).
PARSER_VERSION = 2  # 2: read with icalendar and recurring-ical-events


def calendar_result(entries: list[CalendarPost], title: str | None, url: str) -> dict:
    """A calendar subscription's fetch result. Tasks are its posts ("items"); events take place when
    scheduled, so they aren't posts and are kept apart ("events") to act only as stop times."""
    entries = sorted(entries, key=lambda entry: entry.get("published") or 0, reverse=True)
    name = title or urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]) or "Calendar"
    return {"title": name, "site": url, "icon": None, "complete": True, "parser": PARSER_VERSION,
            "items": [e for e in entries if e["calendar_kind"] != "event"][:MAX_ENTRIES],
            "events": [e for e in entries if e["calendar_kind"] == "event"][:MAX_ENTRIES]}
